"""Minimal full_recent gate for lifecycle Shadow MPV before full multi-window L3."""
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
from afuture.directional_shadow_lifecycle import build_causal_remaining_horizon_panel
from afuture.directional_shadow_mpv_robustness import ShadowMPVDirectionalProductionAcceptance
from afuture.directional_shadow_outcomes import build_shadow_signal_outcomes

import evaluate_directional_production_mechanics as production
import evaluate_directional_shadow_mpv as one_day
import evaluate_return_target_specific as specific


def _one_full_recent(
    simulator,
    specific_raw: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    cost_bps: float,
    prepared,
) -> dict:
    window = weights.loc[pd.Timestamp("2024-08-21") : pd.Timestamp("2026-08-20")]
    result = simulator.simulate(
        specific_raw,
        window,
        cost_bps=cost_bps,
        prepared=prepared,
    )
    return production._result_stats(result)


def evaluate(specific_raw: pd.DataFrame, baseline_weights_raw: pd.DataFrame) -> dict:
    weights = one_day._wide_weights(baseline_weights_raw)
    _close, _gap, intraday, _selection, _quality = specific.build_roll_safe_execution_returns(
        specific_raw
    )
    outcomes = build_shadow_signal_outcomes(weights, intraday)
    horizons = build_causal_remaining_horizon_panel(weights)
    baseline, reproduction = one_day._baseline_reproduction(specific_raw, weights)
    if not all(reproduction.values()):
        return {
            "baseline_reproduction": reproduction,
            "passed": False,
            "reasons": ["baseline_reproduction_failed"],
            "baseline": baseline,
        }

    base_sim = ShadowMPVDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=production.INITIAL_CAPITAL,
            margin_rate_proxy=production.BASE_MARGIN_PROXY,
        ),
        shadow_outcomes=outcomes,
        remaining_horizon_panel=horizons,
    )
    stress_sim = ShadowMPVDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=production.INITIAL_CAPITAL,
            margin_rate_proxy=production.STRESS_MARGIN_PROXY,
        ),
        shadow_outcomes=outcomes,
        remaining_horizon_panel=horizons,
    )
    prepared = base_sim.prepare_contracts(specific_raw)
    base = _one_full_recent(
        base_sim,
        specific_raw,
        weights,
        cost_bps=production.BASE_COST_BPS,
        prepared=prepared,
    )
    stress = _one_full_recent(
        stress_sim,
        specific_raw,
        weights,
        cost_bps=production.STRESS_COST_BPS,
        prepared=prepared,
    )
    reasons: list[str] = []
    if base["annualized_return"] < 0.80:
        reasons.append("base_full_recent_below_80pct")
    if stress["annualized_return"] < 0.80:
        reasons.append("stress_full_recent_below_80pct")
    if stress["max_drawdown"] < -0.30:
        reasons.append("stress_full_recent_dd_exceeds_30pct")
    if base["halted"]:
        reasons.append("base_full_recent_halted")
    if stress["halted"]:
        reasons.append("stress_full_recent_halted")
    if stress["max_realized_gross_notional_ratio"] > 2.0 + 1e-10:
        reasons.append("stress_realized_gross_above_2x")
    return {
        "role": "lifecycle Shadow MPV minimal full_recent Production gate",
        "baseline_reproduction": reproduction,
        "base": base,
        "stress": stress,
        "passed": not reasons,
        "reasons": reasons,
        "shadow_outcome_rows": int(len(outcomes)),
    }


def main() -> None:
    runtime = Path("runtime")
    report = evaluate(
        pd.read_csv(runtime / "return_target_specific_contracts.csv"),
        pd.read_csv(runtime / "execution_aligned_weights.csv"),
    )
    output = runtime / "shadow_lifecycle_mpv_quick_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
