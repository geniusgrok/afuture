"""Fixed Production L3 for the structurally complete DCE 60m OI information surface.

The rule is unchanged from the validated 9-product and 13-product OI candidates. This
candidate adds the five DCE roots B/CS/EB/LH/PG because they are exactly the remaining
DCE products in the frozen 50-product universe and independent coverage collection showed
complete prior/recent 60m history. No product is selected from strategy outcome.
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

import evaluate_directional_60m_oi_confirmation as oi9
import evaluate_directional_production_mechanics as mechanics
from afuture.directional_60m_oi_all_dce import (
    ALL_DCE_OI_PRODUCTS,
    REMAINING_DCE_PRODUCTS,
    audit_remaining_dce_coverage,
    build_all_dce_candidate_weights,
)
from afuture.directional_60m_oi_dce_extension import (
    DCE_EXTENSION_PRODUCTS,
    build_extended_candidate_weights,
)

REFERENCE_13_OI_BASE = 1.01168415
REFERENCE_13_OI_STRESS = 0.55607118
REFERENCE_13_OI_STRESS_NET_ALPHA_TURNOVER_BPS = 22.897842


def _economics(daily: pd.DataFrame, events: pd.DataFrame, *, cost_bps: float) -> dict:
    turnover = float(daily["turnover_notional"].sum()) if not daily.empty else 0.0
    gross = 0.0
    if not events.empty and {"kind", "gross_pnl"}.issubset(events.columns):
        gross = float(
            events.loc[events["kind"].astype(str) == "pnl", "gross_pnl"]
            .astype(float)
            .sum()
        )
    cost = turnover * float(cost_bps) / 10000.0
    net = gross - cost
    return {
        "gross_signal_pnl": gross,
        "turnover_notional": turnover,
        "transaction_cost": cost,
        "net_alpha": net,
        "net_alpha_per_turnover_bps": (
            net / turnover * 10000.0 if turnover > 0.0 else 0.0
        ),
    }


def _final_gate(*, base: dict, stress: dict, economics: dict) -> dict:
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
    if float(economics["net_alpha_per_turnover_bps"]) <= 12.2556:
        reasons.append("stress_net_alpha_per_turnover_not_above_baseline")
    return {"passed": not reasons, "reasons": reasons}


def evaluate(
    *,
    specific_raw: pd.DataFrame,
    base_weights: pd.DataFrame,
    original_60m: pd.DataFrame,
    first_extension_60m: pd.DataFrame,
    remaining_60m: pd.DataFrame,
) -> dict:
    coverage = audit_remaining_dce_coverage(
        calendar=specific_raw[["date", "product"]].copy(),
        remaining_bars=remaining_60m,
        threshold=0.80,
    )
    if not coverage["passed"]:
        raise RuntimeError(f"remaining DCE coverage gate failed: {coverage}")

    bars13 = pd.concat([original_60m, first_extension_60m], ignore_index=True)
    weights13, _ = build_extended_candidate_weights(
        raw_weights=base_weights,
        bars_60m=bars13,
    )
    all_bars = pd.concat([bars13, remaining_60m], ignore_index=True)
    weights_all, lagged = build_all_dce_candidate_weights(
        raw_weights=base_weights,
        bars_60m=all_bars,
    )

    untouched = [
        column
        for column in base_weights.columns
        if column not in REMAINING_DCE_PRODUCTS
    ]
    if not weights_all[untouched].equals(weights13[untouched]):
        raise RuntimeError("all-DCE extension changed pre-existing OI target behavior")
    if bool(
        (weights_all.abs().sum(axis=1) > base_weights.abs().sum(axis=1) + 1e-10).any()
    ):
        raise RuntimeError("all-DCE OI extension increased raw target gross")

    production = mechanics.evaluate_with_weights(specific_raw, weights_all)
    base = production["base"]
    stress = production["stress"]
    economics = {
        "base": _economics(
            production["_base_daily"],
            production["_base_events"],
            cost_bps=mechanics.BASE_COST_BPS,
        ),
        "stress": _economics(
            production["_stress_daily"],
            production["_stress_events"],
            cost_bps=mechanics.STRESS_COST_BPS,
        ),
    }
    sw = stress["windows"]
    independent = {
        "stress_delta_vs_13product_oi": float(
            sw["full_recent"]["annualized_return"]
        ) - REFERENCE_13_OI_STRESS,
        "base_delta_vs_13product_oi": float(
            base["windows"]["full_recent"]["annualized_return"]
        ) - REFERENCE_13_OI_BASE,
        "stress_net_alpha_turnover_delta_bps_vs_13product_oi": float(
            economics["stress"]["net_alpha_per_turnover_bps"]
        ) - REFERENCE_13_OI_STRESS_NET_ALPHA_TURNOVER_BPS,
        "stress_full_recent_not_worse": float(
            sw["full_recent"]["annualized_return"]
        ) >= REFERENCE_13_OI_STRESS,
        "stress_dd_within_30pct": float(sw["full_recent"]["max_drawdown"]) >= -0.30,
        "validation_positive": float(sw["validation"]["annualized_return"]) > 0.0,
        "oos_positive": float(sw["oos"]["annualized_return"]) > 0.0,
    }
    independent["passed"] = bool(
        independent["stress_full_recent_not_worse"]
        and independent["stress_dd_within_30pct"]
        and independent["validation_positive"]
        and independent["oos_positive"]
    )

    full_index = base_weights.loc[
        pd.Timestamp("2024-08-21"):pd.Timestamp("2026-08-20")
    ].index
    activity = {}
    for product in REMAINING_DCE_PRODUCTS:
        series = lagged.reindex(index=full_index)[product].fillna(0.0)
        activity[product] = int((series.abs() > 0.0).sum())

    return {
        "role": "structurally complete DCE plus TA 60m OI Production L3",
        "production_wiring": False,
        "parameter_search": False,
        "supported_products": list(ALL_DCE_OI_PRODUCTS),
        "remaining_extension_products": list(REMAINING_DCE_PRODUCTS),
        "coverage_gate": coverage,
        "decision_rule_unchanged": True,
        "artifact_lineage": {
            "original_prior": 9516473115,
            "original_recent": 9516472727,
            "first_extension_prior": 9524037509,
            "first_extension_recent": 9524033190,
            "remaining_prior": 9525335945,
            "remaining_recent": 9525324355,
        },
        "candidate": {"base": base, "stress": stress},
        "economics": economics,
        "independent_improvement_gate": independent,
        "promotion_gate": _final_gate(
            base=base,
            stress=stress,
            economics=economics["stress"],
        ),
        "remaining_flow_active_days_full_recent": activity,
        "constraints": {
            "target_and_realized_gross_cap": 2.0,
            "hard_margin_ratio": 0.35,
            "min_available_ratio": 0.25,
            "daily_loss_ratio": 0.05,
            "total_drawdown_ratio": 0.30,
            "max_contract_volume": 35,
            "reduction_first_unchanged": True,
        },
    }


def main() -> None:
    runtime = Path("runtime")
    paths = {
        "specific": runtime / "return_target_specific_contracts.csv",
        "weights": runtime / "execution_aligned_weights.csv",
        "prior": runtime / "prior_two_year_broad_60m.csv",
        "recent": runtime / "two_year_broad_60m.csv",
        "ext_prior": runtime / "dce_keymonth_prior_60m.csv",
        "ext_recent": runtime / "dce_keymonth_recent_60m.csv",
        "remaining_prior": runtime / "dce_remaining_prior_60m.csv",
        "remaining_recent": runtime / "dce_remaining_recent_60m.csv",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise SystemExit(f"all-DCE OI inputs missing: {missing}")
    report = evaluate(
        specific_raw=pd.read_csv(paths["specific"]),
        base_weights=oi9.load_frozen_weights(paths["weights"]),
        original_60m=oi9.load_60m([paths["prior"], paths["recent"]]),
        first_extension_60m=oi9.load_60m([paths["ext_prior"], paths["ext_recent"]]),
        remaining_60m=oi9.load_60m([
            paths["remaining_prior"], paths["remaining_recent"]
        ]),
    )
    output = runtime / "stress80_60m_oi_all_dce_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
