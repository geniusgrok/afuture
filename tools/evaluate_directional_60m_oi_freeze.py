"""Fixed Production L3 for 60m OI confirmation plus freeze-new-risk defense.

The 9-product 60m OI overlay is frozen from workflow 32719520981. This evaluator changes
no 60m rule and no hard risk threshold. It first reproduces that known Production result,
then replaces only the existing global 0.25 defensive scaling response with the research
freeze-new-risk adapter while keeping the exact same OI-confirmed weight path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
from afuture.directional_freeze_new_risk import (
    FreezeNewRiskDirectionalProductionAcceptance,
)

EXPECTED_OI_BASE = 1.04613625
EXPECTED_OI_STRESS = 0.39609479
LINEAGE_TOLERANCE = 5e-5
BASELINE_NET_ALPHA_PER_TURNOVER_BPS = 12.2556


def _economics(daily: pd.DataFrame, events: pd.DataFrame, *, cost_bps: float) -> dict:
    turnover = float(daily["turnover_notional"].sum()) if not daily.empty else 0.0
    gross = 0.0
    if not events.empty and {"kind", "gross_pnl"}.issubset(events.columns):
        gross = float(
            events.loc[events["kind"].astype(str) == "pnl", "gross_pnl"].astype(float).sum()
        )
    cost = turnover * float(cost_bps) / 10000.0
    net = gross - cost
    return {
        "gross_signal_pnl": gross,
        "turnover_notional": turnover,
        "transaction_cost": cost,
        "net_alpha": net,
        "net_alpha_per_turnover_bps": (net / turnover * 10000.0 if turnover > 0.0 else 0.0),
    }


def _promotion_gate(base: dict, stress: dict, economics: dict) -> dict:
    reasons: list[str] = []
    bw = base["windows"]
    sw = stress["windows"]
    if float(bw["full_recent"]["annualized_return"]) < 0.80:
        reasons.append("base_full_recent_below_80pct")
    if float(sw["full_recent"]["annualized_return"]) < 0.80:
        reasons.append("stress_full_recent_below_80pct")
    for label, windows in (("base", bw), ("stress", sw)):
        full = windows["full_recent"]
        if float(full["max_drawdown"]) < -0.30 - 1e-12:
            reasons.append(f"{label}_full_recent_dd_exceeds_30pct")
        if bool(full.get("halted", False)):
            reasons.append(f"{label}_full_recent_halted")
        if float(full.get("max_realized_gross_notional_ratio", 0.0)) > 2.0 + 1e-10:
            reasons.append(f"{label}_gross_exceeds_2x")
        if int(full.get("margin_reject_days", 0)) != 0:
            reasons.append(f"{label}_margin_rejects_nonzero")
    for window in ("validation", "oos"):
        item = sw[window]
        if float(item["annualized_return"]) <= 0.0:
            reasons.append(f"stress_{window}_not_positive")
        if float(item["max_drawdown"]) < -0.30 - 1e-12:
            reasons.append(f"stress_{window}_dd_exceeds_30pct")
        if bool(item.get("halted", False)):
            reasons.append(f"stress_{window}_halted")
    if float(economics["net_alpha_per_turnover_bps"]) <= BASELINE_NET_ALPHA_PER_TURNOVER_BPS:
        reasons.append("stress_net_alpha_per_turnover_not_above_baseline")
    return {"passed": not reasons, "reasons": reasons}


def evaluate(
    *,
    specific_raw: pd.DataFrame,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
) -> dict:
    oi_weights, _lagged = oi_gate.build_candidate_weights(base_weights, bars_60m)

    # Reproduce the already-established OI-only Production candidate before combining
    # anything with it. This prevents branch drift from being mistaken for improvement.
    oi_only = mechanics.evaluate_with_weights(specific_raw, oi_weights)
    oi_lineage = {
        "base": abs(
            float(oi_only["base"]["windows"]["full_recent"]["annualized_return"]) - EXPECTED_OI_BASE
        )
        <= LINEAGE_TOLERANCE,
        "stress": abs(
            float(oi_only["stress"]["windows"]["full_recent"]["annualized_return"])
            - EXPECTED_OI_STRESS
        )
        <= LINEAGE_TOLERANCE,
    }
    if not all(oi_lineage.values()):
        raise RuntimeError(f"60m OI Production lineage reproduction failed: {oi_lineage}")

    base_sim = FreezeNewRiskDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.BASE_MARGIN_PROXY,
        )
    )
    stress_sim = FreezeNewRiskDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.STRESS_MARGIN_PROXY,
        )
    )
    prepared = base_sim.prepare_contracts(specific_raw)
    base, base_daily, base_events = mechanics._simulation_report(
        base_sim,
        specific_raw,
        oi_weights,
        prepared=prepared,
        cost_bps=mechanics.BASE_COST_BPS,
        margin_rate_proxy=mechanics.BASE_MARGIN_PROXY,
    )
    stress, stress_daily, stress_events = mechanics._simulation_report(
        stress_sim,
        specific_raw,
        oi_weights,
        prepared=prepared,
        cost_bps=mechanics.STRESS_COST_BPS,
        margin_rate_proxy=mechanics.STRESS_MARGIN_PROXY,
    )
    economics = {
        "base": _economics(base_daily, base_events, cost_bps=mechanics.BASE_COST_BPS),
        "stress": _economics(stress_daily, stress_events, cost_bps=mechanics.STRESS_COST_BPS),
    }
    return {
        "role": "60m OI confirmation plus freeze-new-risk Production L3",
        "parameter_search": False,
        "production_wiring": False,
        "supported_products": list(oi_gate.SUPPORTED_PRODUCTS),
        "oi_only_lineage_reproduction": oi_lineage,
        "oi_only_reference": {
            "base_full_recent": float(
                oi_only["base"]["windows"]["full_recent"]["annualized_return"]
            ),
            "stress_full_recent": float(
                oi_only["stress"]["windows"]["full_recent"]["annualized_return"]
            ),
        },
        "freeze_rule": {
            "lookback_days": 2,
            "completed_loss_trigger": 0.02,
            "completed_two_day_vol_trigger": 0.03,
            "global_point25_scaling": False,
            "freeze_new_and_same_sign_increases": True,
            "reductions_exits_reversals_rolls_bypass": True,
        },
        "constraints": {
            "target_and_realized_gross_cap": 2.0,
            "hard_margin_ratio": 0.35,
            "min_available_ratio": 0.25,
            "daily_loss_ratio": 0.05,
            "total_drawdown_ratio": 0.30,
            "max_contract_volume": 35,
            "reduction_first_unchanged": True,
        },
        "candidate": {"base": base, "stress": stress},
        "economics": economics,
        "promotion_gate": _promotion_gate(base, stress, economics["stress"]),
    }


def main() -> None:
    runtime = Path("runtime")
    required = {
        "specific": runtime / "return_target_specific_contracts.csv",
        "weights": runtime / "execution_aligned_weights.csv",
        "prior": runtime / "prior_two_year_broad_60m.csv",
        "recent": runtime / "two_year_broad_60m.csv",
    }
    missing = [str(path) for path in required.values() if not path.exists()]
    if missing:
        raise SystemExit(f"60m OI freeze inputs missing: {missing}")
    report = evaluate(
        specific_raw=pd.read_csv(required["specific"]),
        base_weights=oi_gate.load_frozen_weights(required["weights"]),
        bars_60m=oi_gate.load_60m([required["prior"], required["recent"]]),
    )
    output = runtime / "stress80_60m_oi_freeze_production_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
