"""Final fixed-input historical-research evaluator for the Stress-80 candidate.

This module is intentionally self-contained relative to the clean promotion branch. It
rebuilds the frozen candidate from immutable evidence and can evaluate one scenario/window
at a time so the final verification workflow can run account simulations in parallel.

Fixed composition (no fitted parameter in this stage):
1. frozen 9-product completed D -> D+1 60m Price x OI evidence;
2. entries, same-sign adds and reversals into a new side require OI confirmation; an
   unconfirmed reversal exits to flat;
3. archived 20 completed sessions / 3-session benefit / 15bp cost eligibility;
4. tracking-first / turnover-second reallocation among approved survivors while restoring
   the confirmed OI gross;
5. strict freeze-new-risk only when completed account drawdown reaches
   30% hard-DD - 5% daily-loss reserve = 25%.

Hard Production constraints and Broker/RiskManager authority are unchanged. Retained
payload role names do not imply live runtime wiring: this file is an offline
historical-research entrypoint and does not authorize live use, increased risk, or
prospective evidence. It is not imported by live runtime wiring.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from io import BytesIO
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluate_directional_60m_oi_confirmation as oi_gate
import evaluate_directional_production_mechanics as mechanics

from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_drawdown_reserve_freeze import (
    DrawdownReserveFreezeDirectionalProductionAcceptance,
)
from afuture.directional_stress90_policy import (
    STRESS90_POLICY,
    build_stress90_candidate_path,
    candidate_weight_digest,
)
from afuture.provenance import ProvenanceError, verify_frozen_input_files

BASELINE_NET_ALPHA_PER_TURNOVER_BPS = 12.2556
MAX_GROSS = 2.0
HARD_MARGIN = 0.35
MIN_AVAILABLE = 0.25
DAILY_LOSS = 0.05
HARD_DRAWDOWN = 0.30
DRAWDOWN_RESERVE = HARD_DRAWDOWN - DAILY_LOSS
MAX_LOTS = 35
FIXED_INPUT_SHA256 = {
    "broad_daily_universe.csv": "c1d46bc113a79bd2e000bf5d66ee53c59337eda750b2d3b1409e40f3f4833d0f",
    "return_target_specific_contracts.csv": "f8b3f4232cb1bc9eaed65a4401dff6bed121faee064252727202874ad7d53c64",
    "execution_aligned_weights.csv": "250a55c1c18ecd3754f489136515275eec3a48d505fd41026d68ab43924e08c1",
    "prior_two_year_broad_60m.csv": "3351aa3ae8dec0cf0e9b9a64026181ac8ff2cc22858c442821fe3c94767036b1",
    "two_year_broad_60m.csv": "5faf112bb69dd5ddf48ed34419e2046b1c6bdd651b595ca46a46804d8317a27b",
}
FIXED_INPUT_BASENAMES = tuple(sorted(FIXED_INPUT_SHA256))
FIXED_INPUT_SIZE_BYTES = MappingProxyType(
    {
        "broad_daily_universe.csv": 3_278_200,
        "execution_aligned_weights.csv": 109_977,
        "prior_two_year_broad_60m.csv": 3_698_425,
        "return_target_specific_contracts.csv": 40_842_073,
        "two_year_broad_60m.csv": 4_080_136,
    }
)
HISTORICAL_RESEARCH_COMMIT = "9c51195042393304eb05d783d1895a165f99b0a7"


def historical_research_metadata() -> dict[str, str | bool]:
    """Return the fixed authorization boundary for archived evaluator evidence."""
    return {
        "historical_replay_commit": HISTORICAL_RESEARCH_COMMIT,
        "evidence_scope": "historical_research_only",
        "live_authorized": False,
        "risk_increase_authorized": False,
        "prospective_evidence": False,
    }


def validate_fixed_input_manifest(
    manifest: Iterable[Mapping[str, object]],
) -> list[dict[str, str | int]]:
    """Require the retained frozen research manifest to match its exact inputs."""
    entries: dict[str, dict[str, str | int]] = {}
    for raw in manifest:
        if set(raw) != {"basename", "sha256", "size_bytes"}:
            raise ValueError("fixed input manifest entry is invalid")
        basename = raw["basename"]
        digest = raw["sha256"]
        size_bytes = raw["size_bytes"]
        if not isinstance(basename, str) or basename in entries:
            raise ValueError("fixed input manifest basenames are invalid")
        expected_digest = FIXED_INPUT_SHA256.get(basename)
        expected_size = FIXED_INPUT_SIZE_BYTES.get(basename)
        if expected_digest is None or expected_size is None:
            raise ValueError(f"unexpected fixed input file: {basename}")
        if digest != expected_digest:
            raise ValueError(f"fixed input SHA-256 mismatch: {basename}")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise ValueError(f"fixed input size is invalid: {basename}")
        if size_bytes != expected_size:
            raise ValueError(f"fixed input size mismatch: {basename}")
        entries[basename] = {
            "basename": basename,
            "sha256": expected_digest,
            "size_bytes": expected_size,
        }
    if set(entries) != set(FIXED_INPUT_SHA256):
        raise ValueError("fixed input manifest basenames are invalid")
    return [entries[name] for name in sorted(entries)]


def continuous_close_panel(raw: pd.DataFrame, products: list[str]) -> pd.DataFrame:
    """Build the causal continuous close panel used only by the frozen cost eligibility."""
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "close"])
    frame = frame[frame["close"] > 0.0]
    frame = frame.drop_duplicates(["date", "product"], keep="last")
    close = frame.pivot(index="date", columns="product", values="close").sort_index()
    missing = sorted(set(products) - set(close.columns))
    if missing:
        raise ValueError(f"continuous close history missing products: {missing}")
    return close.reindex(columns=products).astype(float)


def build_final_candidate_weights(
    *,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
    continuous_raw: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    """Rebuild the final candidate using only the frozen causal evidence pipeline."""
    flow = oi_gate.build_daily_price_oi_flow(bars_60m)
    lagged = oi_gate.lag_flow_to_target_days(
        flow,
        target_days=base_weights.index,
        products=base_weights.columns,
    )
    close = continuous_close_panel(continuous_raw, list(base_weights.columns))
    path = build_stress90_candidate_path(
        base_weights=base_weights,
        completed_close_prices=close,
        confirming_flow=lagged.reindex(columns=STRESS90_POLICY.oi_products),
    )
    confirmed = path.oi_confirmed_weights
    approved = path.cost_approved_weights
    candidate = path.survivor_weights
    candidate = (
        candidate.reindex(
            index=base_weights.index,
            columns=base_weights.columns,
        )
        .fillna(0.0)
        .astype(float)
    )

    if not np.isfinite(candidate.to_numpy()).all():
        raise AssertionError("final candidate contains non-finite weights")
    if bool((candidate.abs().sum(axis=1) > MAX_GROSS + 1e-10).any()):
        raise AssertionError("final candidate exceeds 2x gross")
    if bool((candidate.abs().sum(axis=1) > confirmed.abs().sum(axis=1) + 1e-10).any()):
        raise AssertionError("final survivor allocation exceeds confirmed OI gross")

    digest = candidate_weight_digest(candidate)
    full = candidate.loc[pd.Timestamp("2024-08-21") : pd.Timestamp("2026-08-20")]
    audit = {
        "candidate_weight_sha256": digest,
        "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
        "last_daily_decision_digest": (
            path.decisions[-1].daily_decision_digest if path.decisions else None
        ),
        "supported_products": list(oi_gate.SUPPORTED_PRODUCTS),
        "confirmed_turnover_full_path": weight_turnover(confirmed),
        "approved_turnover_full_path": weight_turnover(approved),
        "candidate_turnover_full_path": weight_turnover(candidate),
        "candidate_average_gross_full_recent": float(full.abs().sum(axis=1).mean()),
        "candidate_max_gross": float(candidate.abs().sum(axis=1).max()),
    }
    return candidate, audit


def weight_turnover(weights: pd.DataFrame) -> float:
    turnover = weights.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = float(weights.iloc[0].abs().sum())
    return float(turnover.sum())


def economics(daily: pd.DataFrame, events: pd.DataFrame, *, cost_bps: float) -> dict:
    turnover = float(daily["turnover_notional"].sum()) if not daily.empty else 0.0
    gross = 0.0
    if not events.empty and {"kind", "gross_pnl"}.issubset(events.columns):
        gross = float(
            events.loc[events["kind"].astype(str) == "pnl", "gross_pnl"].astype(float).sum()
        )
    transaction_cost = turnover * float(cost_bps) / 10000.0
    net = gross - transaction_cost
    return {
        "gross_signal_pnl": gross,
        "turnover_notional": turnover,
        "transaction_cost": transaction_cost,
        "net_alpha": net,
        "net_alpha_per_turnover_bps": (net / turnover * 10000.0 if turnover > 0.0 else 0.0),
    }


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
        mechanics.BASE_MARGIN_PROXY if scenario == "base" else mechanics.STRESS_MARGIN_PROXY
    )
    cost_bps = mechanics.BASE_COST_BPS if scenario == "base" else mechanics.STRESS_COST_BPS
    simulator = DrawdownReserveFreezeDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=margin_proxy,
        )
    )
    prepared = simulator.prepare_contracts(specific_raw)
    start, end = mechanics.WINDOWS[window]
    result = simulator.simulate(
        specific_raw,
        candidate_weights.loc[pd.Timestamp(start) : pd.Timestamp(end)].copy(),
        cost_bps=cost_bps,
        prepared=prepared,
    )
    return {
        "scenario": scenario,
        "window": window,
        "stats": mechanics._result_stats(result),
        "economics": economics(result.daily, result.events, cost_bps=cost_bps),
    }


def promotion_gate(results: dict[tuple[str, str], dict]) -> dict:
    """Predeclared final promotion gate; prior1/prior2 are evidence, not promotion filters."""
    required = [
        ("base", "full_recent"),
        ("stress", "train"),
        ("stress", "validation"),
        ("stress", "oos"),
        ("stress", "full_recent"),
    ]
    missing = [key for key in required if key not in results]
    if missing:
        raise ValueError(f"promotion results missing: {missing}")

    reasons: list[str] = []
    base = results[("base", "full_recent")]["stats"]
    stress_full = results[("stress", "full_recent")]["stats"]
    if float(base["annualized_return"]) < 0.80:
        reasons.append("base_full_recent_below_80pct")
    if float(stress_full["annualized_return"]) < 0.80:
        reasons.append("stress_full_recent_below_80pct")

    for scenario, window in required:
        item = results[(scenario, window)]["stats"]
        label = f"{scenario}_{window}"
        if float(item["max_drawdown"]) < -HARD_DRAWDOWN - 1e-12:
            reasons.append(f"{label}_dd_exceeds_30pct")
        if bool(item.get("halted", False)):
            reasons.append(f"{label}_halted")
        if float(item.get("max_realized_gross_notional_ratio", 0.0)) > MAX_GROSS + 1e-10:
            reasons.append(f"{label}_gross_exceeds_2x")
        if int(item.get("margin_reject_days", 0)) != 0:
            reasons.append(f"{label}_margin_rejects_nonzero")

    for window in ("train", "validation", "oos"):
        if float(results[("stress", window)]["stats"]["annualized_return"]) <= 0.0:
            reasons.append(f"stress_{window}_not_positive")
    stress_economics = results[("stress", "full_recent")]["economics"]
    if float(stress_economics["net_alpha_per_turnover_bps"]) <= BASELINE_NET_ALPHA_PER_TURNOVER_BPS:
        reasons.append("stress_net_alpha_per_turnover_not_above_baseline")
    return {"passed": not reasons, "reasons": reasons}


def _load_inputs(
    runtime: Path,
    *,
    expected_sha256: Mapping[str, str] = FIXED_INPUT_SHA256,
):
    try:
        inputs, input_manifest = verify_frozen_input_files(runtime, expected_sha256)
    except ProvenanceError as exc:
        raise SystemExit(f"Stress80 final input verification failed: {exc}") from exc
    specific = pd.read_csv(BytesIO(inputs["return_target_specific_contracts.csv"]))
    continuous = pd.read_csv(BytesIO(inputs["broad_daily_universe.csv"]))
    base_weights = oi_gate.load_frozen_weights(BytesIO(inputs["execution_aligned_weights.csv"]))
    bars = oi_gate.load_60m(
        [
            BytesIO(inputs["prior_two_year_broad_60m.csv"]),
            BytesIO(inputs["two_year_broad_60m.csv"]),
        ]
    )
    return specific, continuous, base_weights, bars, input_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("base", "stress"), required=True)
    parser.add_argument("--window", choices=tuple(mechanics.WINDOWS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    runtime = Path("runtime")
    specific, continuous, base_weights, bars, input_manifest = _load_inputs(runtime)
    candidate_weights, audit = build_final_candidate_weights(
        base_weights=base_weights,
        bars_60m=bars,
        continuous_raw=continuous,
    )
    result = evaluate_window(
        specific_raw=specific,
        candidate_weights=candidate_weights,
        scenario=args.scenario,
        window=args.window,
    )
    payload = {
        "role": "final fixed Stress80 Production evidence",
        **historical_research_metadata(),
        "parameter_search": False,
        "production_wiring": False,
        "candidate": {**historical_research_metadata(), **audit},
        "input_manifest": validate_fixed_input_manifest(input_manifest),
        "result": result,
        "constraints": {
            "target_and_realized_gross_cap": MAX_GROSS,
            "hard_margin_ratio": HARD_MARGIN,
            "min_available_ratio": MIN_AVAILABLE,
            "daily_loss_ratio": DAILY_LOSS,
            "total_drawdown_ratio": HARD_DRAWDOWN,
            "drawdown_reserve_ratio": DRAWDOWN_RESERVE,
            "max_contract_volume": MAX_LOTS,
            "reduction_first_unchanged": True,
            "risk_manager_authority_unchanged": True,
            "broker_ctp_truth_unchanged": True,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
