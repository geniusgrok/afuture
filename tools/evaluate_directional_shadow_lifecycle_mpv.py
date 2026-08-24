"""Fixed-input Production L3 for lifecycle-aware exogenous Shadow MPV allocation.

This is the single predeclared successor to the rejected one-day Shadow MPV candidate.
The daily expected return estimator is unchanged. The only economic change is multiplying
that estimate by a parameter-free causal expected remaining same-sign baseline lifecycle,
learned solely from completed prior baseline signal runs. Transition cost remains fixed at
15bp one-way in both Base and Stress evaluation scenarios.
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
from afuture.directional_shadow_lifecycle import (
    build_causal_remaining_horizon_panel,
    build_completed_signal_runs,
)
from afuture.directional_shadow_mpv_robustness import (
    SHADOW_OBJECTIVE_COST_BPS,
    ShadowMPVDirectionalProductionAcceptance,
)
from afuture.directional_shadow_outcomes import build_shadow_signal_outcomes

import evaluate_directional_production_mechanics as production
import evaluate_directional_shadow_mpv as one_day
import evaluate_return_target_specific as specific


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


def _candidate_simulation(
    specific_raw: pd.DataFrame,
    weights: pd.DataFrame,
    shadow_outcomes: pd.DataFrame,
    horizon_panel: pd.DataFrame,
) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    base_sim = ShadowMPVDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=production.INITIAL_CAPITAL,
            margin_rate_proxy=production.BASE_MARGIN_PROXY,
        ),
        shadow_outcomes=shadow_outcomes,
        remaining_horizon_panel=horizon_panel,
    )
    stress_sim = ShadowMPVDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=production.INITIAL_CAPITAL,
            margin_rate_proxy=production.STRESS_MARGIN_PROXY,
        ),
        shadow_outcomes=shadow_outcomes,
        remaining_horizon_panel=horizon_panel,
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


def evaluate(
    specific_raw: pd.DataFrame,
    baseline_weights_raw: pd.DataFrame,
) -> dict:
    weights = one_day._wide_weights(baseline_weights_raw)
    _close, _gap, intraday, _selection, quality = specific.build_roll_safe_execution_returns(
        specific_raw
    )
    shadow_outcomes = build_shadow_signal_outcomes(weights, intraday)
    horizon_panel = build_causal_remaining_horizon_panel(weights)
    completed_runs = build_completed_signal_runs(weights)
    if shadow_outcomes.empty:
        raise ValueError("fixed baseline produced no exogenous shadow outcomes")

    baseline, reproduction = one_day._baseline_reproduction(specific_raw, weights)
    if not all(reproduction.values()):
        return {
            "role": "Lifecycle Shadow MPV Production L3",
            "baseline_reproduction": reproduction,
            "candidate_evaluated": False,
            "reason": "fixed Production baseline did not reproduce",
            "baseline": baseline,
        }

    candidate, base_daily, base_events, stress_daily, stress_events = _candidate_simulation(
        specific_raw,
        weights,
        shadow_outcomes,
        horizon_panel,
    )
    active = weights.abs() > 1e-15
    active_horizons = horizon_panel.where(active).stack().dropna()
    report = {
        "role": "causal lifecycle-aware exogenous Shadow MPV Production L3",
        "parameter_search": False,
        "production_wiring": False,
        "one_day_predecessor_rejected": True,
        "baseline_reproduction": reproduction,
        "shadow": {
            "candidate_independent": True,
            "account_halt_independent": True,
            "daily_outcome_rule": "sign(validated baseline target) * roll-safe same-day concrete intraday return",
            "outcome_availability_rule": "outcome day strictly before decision day",
            "objective_cost_bps": SHADOW_OBJECTIVE_COST_BPS,
            "outcome_rows": int(len(shadow_outcomes)),
            "outcome_products": int(shadow_outcomes["product"].nunique()),
            "completed_signal_runs": int(len(completed_runs)),
            "lifecycle_rule": "expanding product+direction completed-run mean shrunk to global; prior support=median positive key support; remaining=max(1,total-age+1)",
            "lifecycle_min": float(active_horizons.min()) if len(active_horizons) else 1.0,
            "lifecycle_median": float(active_horizons.median()) if len(active_horizons) else 1.0,
            "lifecycle_mean": float(active_horizons.mean()) if len(active_horizons) else 1.0,
            "lifecycle_max": float(active_horizons.max()) if len(active_horizons) else 1.0,
        },
        "specific_quality": quality,
        "baseline": baseline,
        "candidate": candidate,
        "economics": {
            "base": _economics(base_events, base_daily),
            "stress": _economics(stress_events, stress_daily),
        },
        "promotion_gate": one_day._promotion_gate(candidate),
    }
    return report


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    weights_path = runtime / "execution_aligned_weights.csv"
    missing = [str(path) for path in (specific_path, weights_path) if not path.exists()]
    if missing:
        raise SystemExit(f"Lifecycle Shadow MPV inputs missing: {missing}")
    report = evaluate(pd.read_csv(specific_path), pd.read_csv(weights_path))
    output = runtime / "shadow_lifecycle_mpv_production_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
