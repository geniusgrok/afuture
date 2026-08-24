"""Fixed-input Production L3 for exogenous Shadow MPV capacity allocation.

The directional signal weights remain the validated baseline. A candidate-independent
shadow ledger is built from baseline target direction x roll-safe same-day concrete-
contract intraday return. Outcome D becomes decision evidence only after D completes.
The integer allocator is always optimized against a fixed 15bp one-way transition-cost
hurdle; Base/Stress evaluation cost never changes the decision objective.
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
from afuture.directional_robustness import MarginAwareDirectionalProductionAcceptance
from afuture.directional_shadow_mpv_robustness import (
    SHADOW_OBJECTIVE_COST_BPS,
    ShadowMPVDirectionalProductionAcceptance,
)
from afuture.directional_shadow_outcomes import build_shadow_signal_outcomes

import evaluate_directional_production_mechanics as production
import evaluate_return_target_specific as specific

BASELINE_BASE_ANNUALIZED = 1.090636
BASELINE_STRESS_ANNUALIZED = 0.289559
BASELINE_TOLERANCE = 5e-5


def _wide_weights(frame: pd.DataFrame) -> pd.DataFrame:
    if {"level_0", "level_1", "weight"}.issubset(frame.columns):
        data = frame.rename(columns={"level_0": "date", "level_1": "product"}).copy()
    elif {"date", "product", "weight"}.issubset(frame.columns):
        data = frame.copy()
    else:
        raise ValueError("baseline weights missing date/product/weight columns")
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data["product"] = data["product"].astype(str).str.upper()
    data["weight"] = pd.to_numeric(data["weight"], errors="coerce")
    data = data.dropna(subset=["date", "product", "weight"])
    result = (
        data.pivot_table(index="date", columns="product", values="weight", aggfunc="last")
        .sort_index()
        .fillna(0.0)
    )
    if bool((result.abs().sum(axis=1) > 2.0 + 1e-10).any()):
        raise ValueError("baseline shadow weights exceed 2x gross")
    return result


def _economics(events: pd.DataFrame, daily: pd.DataFrame) -> dict[str, float]:
    summary = summarize_production_attribution(
        daily=daily,
        events=events,
        initial_capital=production.INITIAL_CAPITAL,
    )
    gross = float(summary["alpha"]["gross_signal_pnl"])
    turnover = float(summary["transaction_cost"]["turnover_notional"])
    cost = float(summary["transaction_cost"]["total_cost"])
    net = gross - cost
    return {
        "gross_signal_pnl": gross,
        "turnover_notional": turnover,
        "transaction_cost": cost,
        "net_alpha_pnl": net,
        "net_alpha_per_turnover_bps": net / turnover * 10000.0 if turnover > 0.0 else 0.0,
    }


def _baseline_reproduction(specific_raw: pd.DataFrame, weights: pd.DataFrame) -> tuple[dict, dict]:
    """Reproduce only the principal fixed window; do not rerun seven baseline windows."""
    window = weights.loc[pd.Timestamp("2024-08-21") : pd.Timestamp("2026-08-20")].copy()
    base_sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=production.INITIAL_CAPITAL,
            margin_rate_proxy=production.BASE_MARGIN_PROXY,
        )
    )
    stress_sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=production.INITIAL_CAPITAL,
            margin_rate_proxy=production.STRESS_MARGIN_PROXY,
        )
    )
    prepared = base_sim.prepare_contracts(specific_raw)
    base_result = base_sim.simulate(
        specific_raw,
        window,
        cost_bps=production.BASE_COST_BPS,
        prepared=prepared,
    )
    stress_result = stress_sim.simulate(
        specific_raw,
        window,
        cost_bps=production.STRESS_COST_BPS,
        prepared=prepared,
    )
    base_stats = production._result_stats(base_result)
    stress_stats = production._result_stats(stress_result)
    checks = {
        "base": abs(float(base_stats["annualized_return"]) - BASELINE_BASE_ANNUALIZED) <= BASELINE_TOLERANCE,
        "stress": abs(float(stress_stats["annualized_return"]) - BASELINE_STRESS_ANNUALIZED) <= BASELINE_TOLERANCE,
    }
    return {"base": base_stats, "stress": stress_stats}, checks


def _candidate_simulation(
    specific_raw: pd.DataFrame,
    weights: pd.DataFrame,
    shadow_outcomes: pd.DataFrame,
) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    base_sim = ShadowMPVDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=production.INITIAL_CAPITAL,
            margin_rate_proxy=production.BASE_MARGIN_PROXY,
        ),
        shadow_outcomes=shadow_outcomes,
    )
    stress_sim = ShadowMPVDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=production.INITIAL_CAPITAL,
            margin_rate_proxy=production.STRESS_MARGIN_PROXY,
        ),
        shadow_outcomes=shadow_outcomes,
    )
    prepared = base_sim.prepare_contracts(specific_raw)
    base, base_daily, base_events = production._simulation_report(
        base_sim,
        specific_raw,
        weights,
        prepared=prepared,
        cost_bps=production.BASE_COST_BPS,
        margin_rate_proxy=production.BASE_MARGIN_PROXY,
    )
    stress, stress_daily, stress_events = production._simulation_report(
        stress_sim,
        specific_raw,
        weights,
        prepared=prepared,
        cost_bps=production.STRESS_COST_BPS,
        margin_rate_proxy=production.STRESS_MARGIN_PROXY,
    )
    return {"base": base, "stress": stress}, base_daily, base_events, stress_daily, stress_events


def _promotion_gate(candidate: dict) -> dict:
    base = candidate["base"]["windows"]
    stress = candidate["stress"]["windows"]
    reasons: list[str] = []
    if stress["full_recent"]["annualized_return"] < 0.80:
        reasons.append("stress_full_recent_below_80pct")
    if stress["full_recent"]["max_drawdown"] < -0.30:
        reasons.append("stress_full_recent_dd_exceeds_30pct")
    if base["full_recent"]["annualized_return"] < 0.80:
        reasons.append("base_full_recent_below_80pct")
    for window in ("validation", "oos"):
        if stress[window]["annualized_return"] <= 0.0:
            reasons.append(f"stress_{window}_not_positive")
        if stress[window]["max_drawdown"] < -0.30:
            reasons.append(f"stress_{window}_dd_exceeds_30pct")
        if stress[window]["halted"]:
            reasons.append(f"stress_{window}_halted")
    if stress["full_recent"]["halted"]:
        reasons.append("stress_full_recent_halted")
    if base["full_recent"]["halted"]:
        reasons.append("base_full_recent_halted")
    if stress["full_recent"]["max_realized_gross_notional_ratio"] > 2.0 + 1e-10:
        reasons.append("stress_realized_gross_above_2x")
    return {"passed": not reasons, "reasons": reasons}


def evaluate(
    specific_raw: pd.DataFrame,
    baseline_weights_raw: pd.DataFrame,
) -> dict:
    weights = _wide_weights(baseline_weights_raw)
    _close, _gap, intraday, _selection, quality = specific.build_roll_safe_execution_returns(
        specific_raw
    )
    shadow_outcomes = build_shadow_signal_outcomes(weights, intraday)
    if shadow_outcomes.empty:
        raise ValueError("fixed baseline produced no exogenous shadow outcomes")

    baseline, reproduction = _baseline_reproduction(specific_raw, weights)
    if not all(reproduction.values()):
        return {
            "role": "Shadow MPV Production L3",
            "baseline_reproduction": reproduction,
            "candidate_evaluated": False,
            "reason": "fixed Production baseline did not reproduce",
            "baseline": baseline,
        }

    candidate, base_daily, base_events, stress_daily, stress_events = _candidate_simulation(
        specific_raw,
        weights,
        shadow_outcomes,
    )
    report = {
        "role": "exogenous baseline signal-outcome Shadow MPV Production L3",
        "parameter_search": False,
        "production_wiring": False,
        "baseline_reproduction": reproduction,
        "shadow": {
            "candidate_independent": True,
            "account_halt_independent": True,
            "outcome_rule": "sign(validated baseline target) * roll-safe same-day concrete intraday return",
            "availability_rule": "outcome day strictly before decision day",
            "objective_cost_bps": SHADOW_OBJECTIVE_COST_BPS,
            "rows": int(len(shadow_outcomes)),
            "products": int(shadow_outcomes["product"].nunique()),
            "first": pd.Timestamp(shadow_outcomes["date"].min()).date().isoformat(),
            "last": pd.Timestamp(shadow_outcomes["date"].max()).date().isoformat(),
        },
        "specific_quality": quality,
        "baseline": baseline,
        "candidate": candidate,
        "economics": {
            "base": _economics(base_events, base_daily),
            "stress": _economics(stress_events, stress_daily),
        },
        "promotion_gate": _promotion_gate(candidate),
    }
    return report


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    weights_path = runtime / "execution_aligned_weights.csv"
    missing = [str(path) for path in (specific_path, weights_path) if not path.exists()]
    if missing:
        raise SystemExit(f"Shadow MPV inputs missing: {missing}")
    report = evaluate(pd.read_csv(specific_path), pd.read_csv(weights_path))
    output = runtime / "shadow_mpv_production_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
