"""Fixed-input Production L3 evaluator for the causal MPV integer allocator.

The evaluator reuses the frozen PR #17 execution-aligned product weights and the exact
concrete-contract artifact used by the validated Production baseline. It does not rebuild
or retune Alpha. Every reporting window remains an independent account experiment, while
MPV learns only from Production audit events observed inside that causal simulation.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_attribution import summarize_production_attribution
from afuture.directional_mpv_robustness import MPVDirectionalProductionAcceptance

import evaluate_directional_production_mechanics as mechanics


def load_frozen_weights(path: Path) -> pd.DataFrame:
    rows = pd.read_csv(path)
    required = {"level_0", "level_1", "weight"}
    if not required.issubset(rows.columns):
        raise ValueError(f"frozen weight artifact missing columns: {sorted(required - set(rows.columns))}")
    rows["level_0"] = pd.to_datetime(rows["level_0"], errors="coerce").dt.normalize()
    rows = rows[rows["level_0"].notna()].copy()
    rows["level_1"] = rows["level_1"].astype(str).str.upper()
    rows["weight"] = pd.to_numeric(rows["weight"], errors="coerce").fillna(0.0)
    if rows.duplicated(["level_0", "level_1"]).any():
        raise ValueError("frozen weight artifact contains duplicate date/product rows")
    weights = rows.pivot(index="level_0", columns="level_1", values="weight").fillna(0.0)
    weights.index.name = None
    weights.columns.name = None
    if bool((weights.abs().sum(axis=1) > 2.0 + 1e-10).any()):
        raise ValueError("frozen execution-aligned weights exceed 2x gross")
    return weights.sort_index()


def _turnover_summary(frame: pd.DataFrame) -> dict[str, float]:
    columns = [
        "turnover_roll",
        "turnover_resize",
        "turnover_reversal",
        "turnover_entry_exit",
        "turnover_daily_circuit",
        "turnover_hard_halt",
        "turnover_gross_guard",
    ]
    result = {
        column: float(frame[column].sum()) if column in frame else 0.0
        for column in columns
    }
    result["total"] = (
        float(frame["turnover_notional"].sum())
        if "turnover_notional" in frame
        else 0.0
    )
    result["attributed_total"] = float(sum(result[column] for column in columns))
    return result


def _economic_summary(attribution: dict) -> dict[str, float]:
    gross = float(attribution["alpha"]["gross_signal_pnl"])
    turnover = float(attribution["transaction_cost"]["turnover_notional"])
    cost = float(attribution["transaction_cost"]["total_cost"])
    net = gross - cost
    return {
        "gross_signal_pnl": gross,
        "turnover_notional": turnover,
        "transaction_cost": cost,
        "net_alpha_pnl": net,
        "net_alpha_per_turnover_bps": (net / turnover * 10000.0 if turnover > 0.0 else 0.0),
    }


def evaluate_with_weights(specific_raw: pd.DataFrame, weights: pd.DataFrame) -> dict:
    base_sim = MPVDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.BASE_MARGIN_PROXY,
        )
    )
    stress_sim = MPVDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.STRESS_MARGIN_PROXY,
        )
    )
    prepared = base_sim.prepare_contracts(specific_raw)
    base, base_daily, base_events = mechanics._simulation_report(
        base_sim,
        specific_raw,
        weights,
        prepared=prepared,
        cost_bps=mechanics.BASE_COST_BPS,
        margin_rate_proxy=mechanics.BASE_MARGIN_PROXY,
    )
    stress, stress_daily, stress_events = mechanics._simulation_report(
        stress_sim,
        specific_raw,
        weights,
        prepared=prepared,
        cost_bps=mechanics.STRESS_COST_BPS,
        margin_rate_proxy=mechanics.STRESS_MARGIN_PROXY,
    )
    production_attribution = {
        "base": summarize_production_attribution(
            daily=base_daily,
            events=base_events,
            initial_capital=mechanics.INITIAL_CAPITAL,
        ),
        "stress": summarize_production_attribution(
            daily=stress_daily,
            events=stress_events,
            initial_capital=mechanics.INITIAL_CAPITAL,
        ),
    }
    return {
        "role": "research-only Production MPV integer allocation",
        "strategy_version": "production_mpv_integer_v1",
        "selection_frozen": True,
        "parameter_search": False,
        "production_wiring": False,
        "decision_horizon": "observed completed intraday Production PnL per lot-segment",
        "weight_source": "frozen PR #17 execution_aligned_weights.csv",
        "state_reset_per_window": True,
        "base": base,
        "stress": stress,
        "turnover_attribution": {
            "base": _turnover_summary(base_daily),
            "stress": _turnover_summary(stress_daily),
        },
        "production_attribution": production_attribution,
        "economics": {
            "base": _economic_summary(production_attribution["base"]),
            "stress": _economic_summary(production_attribution["stress"]),
        },
        "constraints": {
            "target_gross_leverage_cap": 2.0,
            "hard_margin_ratio": float(base_sim.config.max_margin_ratio),
            "min_available_ratio": float(base_sim.config.min_available_ratio),
            "max_contract_volume": min(int(base_sim.config.max_contract_volume), 35),
            "reduction_first_unchanged": True,
            "risk_manager_authority_unchanged": True,
        },
        "limitations": [
            "historical broker margin schedules remain unavailable; the established Production L3 margin proxy is unchanged",
            "the first MPV candidate uses completed intraday Production gross PnL plus exact current turnover cost; unsupported risk/correlation penalty weights are not fabricated",
            "each published account window starts flat with fresh account state and therefore also starts without prior-window Production outcome evidence",
            "research results are not live runtime wiring and are not a forecast or guarantee of future return",
        ],
        "_base_daily": base_daily,
        "_stress_daily": stress_daily,
        "_base_events": base_events,
        "_stress_events": stress_events,
    }


def _jsonable(report: dict) -> dict:
    return {key: value for key, value in report.items() if not key.startswith("_")}


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    weights_path = runtime / "execution_aligned_weights.csv"
    missing = [str(path) for path in (specific_path, weights_path) if not path.exists()]
    if missing:
        raise SystemExit(f"MPV Production inputs missing: {missing}")

    report = evaluate_with_weights(
        pd.read_csv(specific_path),
        load_frozen_weights(weights_path),
    )
    report["_base_daily"].to_csv(runtime / "mpv_production_base_daily.csv", index=False)
    report["_stress_daily"].to_csv(runtime / "mpv_production_stress_daily.csv", index=False)
    report["_base_events"].to_csv(runtime / "mpv_production_base_events.csv", index=False)
    report["_stress_events"].to_csv(runtime / "mpv_production_stress_events.csv", index=False)
    output = runtime / "mpv_production_report.json"
    output.write_text(
        json.dumps(_jsonable(report), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
