"""Final fixed-input Production evaluator for the validated Stress90 candidate.

The candidate preserves the Stress80 signal, cost-eligibility, survivor allocation and
hard-gate path.  It adds one causal state: standard target-weight HHI compared with its
strictly prior expanding median.  Weak-leadership days freeze only new entries and
same-sign increases; reduction-first actions remain executable.  The fixed 25% account
drawdown reserve uses the full completed causal account path.

This is an offline Production evidence entrypoint, not live runtime wiring.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluate_directional_production_mechanics as mechanics
import evaluate_directional_stress80_final as stress80
from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_concentration_freeze import (
    ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance,
    target_weight_concentration,
)
from afuture.directional_stress90_gate import evaluate_stress90_gate

EXPECTED_CANDIDATE_WEIGHT_SHA256 = (
    "8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28"
)
EXPECTED_CONSTRAINTS = {
    "target_and_realized_gross_cap": stress80.MAX_GROSS,
    "hard_margin_ratio": stress80.HARD_MARGIN,
    "min_available_ratio": stress80.MIN_AVAILABLE,
    "daily_loss_ratio": stress80.DAILY_LOSS,
    "total_drawdown_ratio": stress80.HARD_DRAWDOWN,
    "drawdown_reserve_ratio": stress80.DRAWDOWN_RESERVE,
    "max_contract_volume": stress80.MAX_LOTS,
    "reduction_first_unchanged": True,
    "broker_ctp_truth_unchanged": True,
    "risk_manager_authority_unchanged": True,
}


def completed_concentrations_before(
    weights: pd.DataFrame,
    *,
    start: pd.Timestamp,
) -> tuple[float, ...]:
    """Seed an independent account with strictly earlier exogenous target states."""
    values: list[float] = []
    for _, row in weights.loc[weights.index < pd.Timestamp(start)].iterrows():
        value = target_weight_concentration(row.to_dict())
        if value is not None:
            values.append(value)
    return tuple(values)


def evaluate_window(
    *,
    specific_raw: pd.DataFrame,
    candidate_weights: pd.DataFrame,
    scenario: str,
    window: str,
) -> dict:
    if scenario not in {"base", "stress"}:
        raise ValueError(f"unsupported scenario: {scenario}")
    if window not in mechanics.WINDOWS:
        raise ValueError(f"unsupported window: {window}")
    margin_proxy = (
        mechanics.BASE_MARGIN_PROXY
        if scenario == "base"
        else mechanics.STRESS_MARGIN_PROXY
    )
    cost_bps = (
        mechanics.BASE_COST_BPS
        if scenario == "base"
        else mechanics.STRESS_COST_BPS
    )
    start, end = mechanics.WINDOWS[window]
    simulator = ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=margin_proxy,
        ),
        completed_concentrations=completed_concentrations_before(
            candidate_weights,
            start=pd.Timestamp(start),
        ),
    )
    prepared = simulator.prepare_contracts(specific_raw)
    result = simulator.simulate(
        specific_raw,
        candidate_weights.loc[start:end].copy(),
        cost_bps=cost_bps,
        prepared=prepared,
    )
    return {
        "scenario": scenario,
        "window": window,
        "stats": mechanics._result_stats(result),
        "economics": stress80.economics(result.daily, result.events, cost_bps=cost_bps),
    }


def assemble_matrix_payload(payloads: Iterable[dict]) -> dict:
    """Validate independently produced window payloads and apply the frozen gate."""
    results: dict[tuple[str, str], dict] = {}
    for payload in payloads:
        if payload.get("role") != "final fixed Stress90 Production evidence":
            raise ValueError("unexpected Stress90 matrix payload role")
        if payload.get("parameter_search") is not False:
            raise ValueError("Stress90 matrix payload permits parameter search")
        if payload.get("production_wiring") is not False:
            raise ValueError("Stress90 matrix payload changes production wiring")
        if payload.get("constraints") != EXPECTED_CONSTRAINTS:
            raise ValueError("Stress90 matrix payload constraints changed")
        digest = payload.get("candidate", {}).get("candidate_weight_sha256")
        if digest != EXPECTED_CANDIDATE_WEIGHT_SHA256:
            raise ValueError("Stress90 matrix candidate digest mismatch")
        result = payload["result"]
        key = (str(result["scenario"]), str(result["window"]))
        if key in results:
            raise ValueError(f"duplicate Stress90 matrix result: {key}")
        results[key] = result

    gate = evaluate_stress90_gate(
        results,
        max_contract_lots=stress80.MAX_LOTS,
    )
    return {
        "role": "assembled final Stress90 Production matrix",
        "candidate_weight_sha256": EXPECTED_CANDIDATE_WEIGHT_SHA256,
        "results": {f"{scenario}/{window}": item for (scenario, window), item in results.items()},
        "gate": gate,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("base", "stress"), required=True)
    parser.add_argument("--window", choices=tuple(mechanics.WINDOWS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    runtime = Path("runtime")
    specific, continuous, base_weights, bars = stress80._load_inputs(runtime)
    candidate, audit = stress80.build_final_candidate_weights(
        base_weights=base_weights,
        bars_60m=bars,
        continuous_raw=continuous,
    )
    if audit["candidate_weight_sha256"] != EXPECTED_CANDIDATE_WEIGHT_SHA256:
        raise AssertionError("final Stress90 candidate weights changed")
    payload = {
        "role": "final fixed Stress90 Production evidence",
        "parameter_search": False,
        "production_wiring": False,
        "candidate": audit,
        "result": evaluate_window(
            specific_raw=specific,
            candidate_weights=candidate,
            scenario=args.scenario,
            window=args.window,
        ),
        "constraints": dict(EXPECTED_CONSTRAINTS),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
