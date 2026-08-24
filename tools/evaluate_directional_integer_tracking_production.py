"""Fixed-input Production L3 for the research-only integer tracking candidate."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import evaluate_directional_production_mechanics as production
from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_integer_tracking import (
    IntegerTrackingDirectionalProductionAcceptance,
)

BASELINE_BASE_ANNUALIZED = 1.090636
BASELINE_STRESS_ANNUALIZED = 0.289559
BASELINE_STRESS_NET_ALPHA_TURNOVER_BPS = 12.2556


def _economics(daily: pd.DataFrame, events: pd.DataFrame, *, cost_bps: float) -> dict:
    turnover = float(daily["turnover_notional"].sum()) if not daily.empty else 0.0
    gross_pnl = 0.0
    if not events.empty and {"kind", "gross_pnl"}.issubset(events.columns):
        gross_pnl = float(
            events.loc[events["kind"].astype(str) == "pnl", "gross_pnl"]
            .astype(float)
            .sum()
        )
    cost = turnover * float(cost_bps) / 10000.0
    net = gross_pnl - cost
    return {
        "gross_signal_pnl": gross_pnl,
        "turnover_notional": turnover,
        "transaction_cost": cost,
        "net_alpha": net,
        "net_alpha_per_turnover_bps": (
            net / turnover * 10000.0 if turnover > 0.0 else 0.0
        ),
    }


def _gate(base: dict, stress: dict, economics: dict) -> dict:
    bw = base["windows"]
    sw = stress["windows"]
    reasons: list[str] = []
    if bw["full_recent"]["annualized_return"] < 0.80:
        reasons.append("base_full_recent_below_80pct")
    if sw["full_recent"]["annualized_return"] < 0.80:
        reasons.append("stress_full_recent_below_80pct")
    for label, windows in (("base", bw), ("stress", sw)):
        full = windows["full_recent"]
        if full["max_drawdown"] < -0.30 - 1e-12:
            reasons.append(f"{label}_full_recent_dd_exceeds_30pct")
        if full["halted"]:
            reasons.append(f"{label}_full_recent_halted")
        if full["max_realized_gross_notional_ratio"] > 2.0 + 1e-9:
            reasons.append(f"{label}_gross_exceeds_2x")
        if full["margin_reject_days"] != 0:
            reasons.append(f"{label}_margin_rejects_nonzero")
    for window in ("validation", "oos"):
        if sw[window]["annualized_return"] <= 0.0:
            reasons.append(f"stress_{window}_not_positive")
        if sw[window]["max_drawdown"] < -0.30 - 1e-12:
            reasons.append(f"stress_{window}_dd_exceeds_30pct")
        if sw[window]["halted"]:
            reasons.append(f"stress_{window}_halted")
    if economics["net_alpha_per_turnover_bps"] <= BASELINE_STRESS_NET_ALPHA_TURNOVER_BPS:
        reasons.append("stress_net_alpha_per_turnover_not_above_baseline")
    return {"passed": not reasons, "reasons": reasons}


def evaluate(specific_raw: pd.DataFrame, weights: pd.DataFrame) -> dict:
    baseline = production.evaluate_with_weights(specific_raw, weights)
    baseline_check = {
        "base": abs(
            baseline["base"]["windows"]["full_recent"]["annualized_return"]
            - BASELINE_BASE_ANNUALIZED
        ) < 5e-6,
        "stress": abs(
            baseline["stress"]["windows"]["full_recent"]["annualized_return"]
            - BASELINE_STRESS_ANNUALIZED
        ) < 5e-6,
    }
    if not all(baseline_check.values()):
        raise RuntimeError(f"fixed baseline reproduction failed: {baseline_check}")

    base_sim = IntegerTrackingDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=production.INITIAL_CAPITAL,
            margin_rate_proxy=production.BASE_MARGIN_PROXY,
        )
    )
    stress_sim = IntegerTrackingDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=production.INITIAL_CAPITAL,
            margin_rate_proxy=production.STRESS_MARGIN_PROXY,
        )
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
    economics = {
        "base": _economics(
            base_daily, base_events, cost_bps=production.BASE_COST_BPS
        ),
        "stress": _economics(
            stress_daily, stress_events, cost_bps=production.STRESS_COST_BPS
        ),
    }
    return {
        "role": "research-only causal integer target tracking Production L3",
        "selection_frozen": True,
        "parameter_search": False,
        "live_runtime_wired": False,
        "objective": [
            "maximize realizable gross inside original target and existing soft margin budget",
            "minimize absolute product-level target tracking error",
        ],
        "baseline_reproduction": baseline_check,
        "baseline": {
            "base": baseline["base"],
            "stress": baseline["stress"],
        },
        "candidate": {"base": base, "stress": stress},
        "economics": economics,
        "promotion_gate": _gate(base, stress, economics["stress"]),
    }


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    weights_path = runtime / "execution_aligned_weights.csv"
    if not specific_path.exists() or not weights_path.exists():
        raise SystemExit("fixed Production L3 inputs are missing")
    specific_raw = pd.read_csv(specific_path)
    long_weights = pd.read_csv(weights_path)
    required = {"level_0", "level_1", "weight"}
    if not required.issubset(long_weights.columns):
        raise SystemExit(f"unexpected weight schema: {list(long_weights.columns)}")
    weights = (
        long_weights.pivot_table(
            index="level_0", columns="level_1", values="weight", aggfunc="last"
        )
        .fillna(0.0)
        .sort_index()
    )
    weights.index = pd.to_datetime(weights.index, errors="coerce")
    weights = weights[~weights.index.isna()].sort_index()
    report = evaluate(specific_raw, weights)
    output = runtime / "directional_integer_tracking_production_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
