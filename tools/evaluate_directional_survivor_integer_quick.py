"""Five-window quick Production gate for survivor-aware nearest integer realization.

The continuous target path is the already-validated 72.242885% tracking-first survivor
candidate. This tool changes only its integer realization by using canonical nearest
floor/ceil recovery before the unchanged soft-margin fitter, one-lot stabilizer and
freeze-new-risk response. No Alpha, survivor, threshold or hard risk parameter changes.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluate_directional_60m_oi_confirmation as oi_gate
import evaluate_directional_60m_oi_freeze as checkpoint
import evaluate_directional_oi_freeze_cost_combo as suppress
import evaluate_directional_production_mechanics as mechanics
import evaluate_directional_turnover_aware_survivor as tracking
from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_survivor_integer_realization import (
    SurvivorIntegerDirectionalProductionAcceptance,
)

TRACKING_STRESS_REFERENCE = 0.72242885
BASELINE_NET_ALPHA_PER_TURNOVER_BPS = 12.2556


def _run_window(
    simulator: SurvivorIntegerDirectionalProductionAcceptance,
    *,
    specific_raw: pd.DataFrame,
    prepared,
    weights: pd.DataFrame,
    window: str,
    cost_bps: float,
):
    start, end = mechanics.WINDOWS[window]
    result = simulator.simulate(
        specific_raw,
        weights.loc[pd.Timestamp(start):pd.Timestamp(end)].copy(),
        cost_bps=cost_bps,
        prepared=prepared,
    )
    return mechanics._result_stats(result), result.daily.copy(), result.events.copy()


def _gate(base_full: dict, stress: dict, economics: dict) -> dict:
    reasons: list[str] = []
    if float(base_full["annualized_return"]) < 0.80:
        reasons.append("base_full_recent_below_80pct")
    for key, item in (("base_full_recent", base_full), *stress.items()):
        if float(item["max_drawdown"]) < -0.30 - 1e-12:
            reasons.append(f"{key}_dd_exceeds_30pct")
        if bool(item.get("halted", False)):
            reasons.append(f"{key}_halted")
        if float(item.get("max_realized_gross_notional_ratio", 0.0)) > 2.0 + 1e-10:
            reasons.append(f"{key}_gross_exceeds_2x")
        if int(item.get("margin_reject_days", 0)) != 0:
            reasons.append(f"{key}_margin_rejects_nonzero")
    if float(stress["full_recent"]["annualized_return"]) < 0.80:
        reasons.append("stress_full_recent_below_80pct")
    for window in ("train", "validation", "oos"):
        if float(stress[window]["annualized_return"]) <= 0.0:
            reasons.append(f"stress_{window}_not_positive")
    if float(economics["net_alpha_per_turnover_bps"]) <= BASELINE_NET_ALPHA_PER_TURNOVER_BPS:
        reasons.append("stress_net_alpha_per_turnover_not_above_baseline")
    return {"passed": not reasons, "reasons": reasons}


def evaluate(
    *,
    specific_raw: pd.DataFrame,
    continuous_raw: pd.DataFrame,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
) -> dict:
    close = suppress.continuous_close_panel(continuous_raw, list(base_weights.columns))
    _oi, _approved, tracking_weights = tracking.build_turnover_aware_weights(
        base_weights=base_weights,
        bars_60m=bars_60m,
        close_prices=close,
    )

    base_sim = SurvivorIntegerDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.BASE_MARGIN_PROXY,
        )
    )
    stress_sim = SurvivorIntegerDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.STRESS_MARGIN_PROXY,
        )
    )
    prepared = base_sim.prepare_contracts(specific_raw)
    base_full, _base_daily, _base_events = _run_window(
        base_sim,
        specific_raw=specific_raw,
        prepared=prepared,
        weights=tracking_weights,
        window="full_recent",
        cost_bps=mechanics.BASE_COST_BPS,
    )

    stress: dict[str, dict] = {}
    stress_full_daily = pd.DataFrame()
    stress_full_events = pd.DataFrame()
    for window in ("train", "validation", "oos", "full_recent"):
        stats, daily, events = _run_window(
            stress_sim,
            specific_raw=specific_raw,
            prepared=prepared,
            weights=tracking_weights,
            window=window,
            cost_bps=mechanics.STRESS_COST_BPS,
        )
        stress[window] = stats
        if window == "full_recent":
            stress_full_daily = daily
            stress_full_events = events

    economics = checkpoint._economics(
        stress_full_daily,
        stress_full_events,
        cost_bps=mechanics.STRESS_COST_BPS,
    )
    gate = _gate(base_full, stress, economics)
    return {
        "role": "five-window quick Production gate for survivor-aware nearest integer realization",
        "parameter_search": False,
        "production_wiring": False,
        "continuous_reference_stress_full_recent": TRACKING_STRESS_REFERENCE,
        "base_full_recent": base_full,
        "stress": stress,
        "economics": economics,
        "delta_vs_72_tracking_stress_annualized": (
            float(stress["full_recent"]["annualized_return"])
            - TRACKING_STRESS_REFERENCE
        ),
        "promotion_gate": gate,
        "constraints": {
            "target_and_realized_gross_cap": 2.0,
            "hard_margin_ratio": 0.35,
            "min_available_ratio": 0.25,
            "daily_loss_ratio": 0.05,
            "total_drawdown_ratio": 0.30,
            "max_contract_volume": 35,
            "reduction_first_unchanged": True,
            "ceil_fraction_rule": "strictly_greater_than_half",
        },
    }


def main() -> None:
    runtime = Path("runtime")
    paths = {
        "specific": runtime / "return_target_specific_contracts.csv",
        "continuous": runtime / "broad_daily_universe.csv",
        "weights": runtime / "execution_aligned_weights.csv",
        "prior": runtime / "prior_two_year_broad_60m.csv",
        "recent": runtime / "two_year_broad_60m.csv",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise SystemExit(f"survivor integer quick inputs missing: {missing}")
    report = evaluate(
        specific_raw=pd.read_csv(paths["specific"]),
        continuous_raw=pd.read_csv(paths["continuous"]),
        base_weights=oi_gate.load_frozen_weights(paths["weights"]),
        bars_60m=oi_gate.load_60m([paths["prior"], paths["recent"]]),
    )
    output = runtime / "stress80_survivor_integer_quick_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
