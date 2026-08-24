"""Fixed Production L3 for lifecycle-defined DCE extension of the 60m OI overlay.

No signal rule, lag, threshold, risk limit or portfolio parameter changes. The only change
from the validated 9-product OI candidate is adding J/JM/L/V because they share the same
DCE 01/05/09 lifecycle and each has >=80% exact daily 60m coverage in both prior/recent
eras. The evaluator verifies that coverage again from frozen artifacts before running.
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
from afuture.directional_60m_oi_dce_extension import (
    DCE_EXTENSION_PRODUCTS,
    EXTENDED_SUPPORTED_PRODUCTS,
    audit_dce_extension_coverage,
    build_extended_candidate_weights,
)

REFERENCE_OI9_BASE = 1.0461362468104771
REFERENCE_OI9_STRESS = 0.39609479470641795
REFERENCE_OI9_STRESS_DD = -0.171416
REFERENCE_OI9_STRESS_NET_ALPHA_TURNOVER_BPS = 16.501499


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


def _promotion_gate(*, base: dict, stress: dict, economics: dict) -> dict:
    reasons: list[str] = []
    bw = base["windows"]
    sw = stress["windows"]
    if float(bw["full_recent"]["annualized_return"]) < 0.80:
        reasons.append("base_full_recent_below_80pct")
    if float(sw["full_recent"]["annualized_return"]) < 0.80:
        reasons.append("stress_full_recent_below_80pct")
    if float(sw["full_recent"]["max_drawdown"]) < -0.30:
        reasons.append("stress_full_recent_dd_above_30pct")
    if bool(sw["full_recent"].get("halted", False)):
        reasons.append("stress_full_recent_halted")
    if float(sw["full_recent"].get("max_realized_gross_notional_ratio", 0.0)) > 2.0 + 1e-10:
        reasons.append("stress_full_recent_gross_above_2x")
    if int(sw["full_recent"].get("margin_reject_days", 0)) > 0:
        reasons.append("stress_full_recent_margin_reject")
    for window in ("validation", "oos"):
        item = sw[window]
        if float(item["annualized_return"]) <= 0.0:
            reasons.append(f"{window}_stress_not_positive")
        if float(item["max_drawdown"]) < -0.30:
            reasons.append(f"{window}_stress_dd_above_30pct")
        if bool(item.get("halted", False)):
            reasons.append(f"{window}_stress_halted")
    return {"passed": not reasons, "reasons": reasons}


def evaluate(
    *,
    specific_raw: pd.DataFrame,
    base_weights: pd.DataFrame,
    original_60m: pd.DataFrame,
    extension_60m: pd.DataFrame,
) -> dict:
    coverage = audit_dce_extension_coverage(
        calendar=specific_raw[["date", "product"]].copy(),
        extension_bars=extension_60m,
        threshold=0.80,
    )
    if not coverage["passed"]:
        raise RuntimeError(f"DCE lifecycle extension coverage gate failed: {coverage}")

    oi9_weights, _ = oi9.build_candidate_weights(base_weights, original_60m)
    combined = pd.concat([original_60m, extension_60m], ignore_index=True)
    extended_weights, lagged = build_extended_candidate_weights(
        raw_weights=base_weights,
        bars_60m=combined,
    )

    # The information extension must not mutate the already-validated 9-product behavior.
    untouched = [
        column
        for column in base_weights.columns
        if column not in DCE_EXTENSION_PRODUCTS
    ]
    if not extended_weights[untouched].equals(oi9_weights[untouched]):
        raise RuntimeError("DCE extension changed non-extension OI target behavior")
    if bool(
        (
            extended_weights.abs().sum(axis=1)
            > base_weights.abs().sum(axis=1) + 1e-10
        ).any()
    ):
        raise RuntimeError("DCE extension increased raw target gross")

    production = mechanics.evaluate_with_weights(specific_raw, extended_weights)
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
    full = stress["windows"]["full_recent"]
    independent_improvement = {
        "stress_annualized_delta_vs_oi9": float(full["annualized_return"]) - REFERENCE_OI9_STRESS,
        "base_annualized_delta_vs_oi9": float(base["windows"]["full_recent"]["annualized_return"]) - REFERENCE_OI9_BASE,
        "stress_net_alpha_turnover_delta_bps_vs_oi9": float(
            economics["stress"]["net_alpha_per_turnover_bps"]
        ) - REFERENCE_OI9_STRESS_NET_ALPHA_TURNOVER_BPS,
        "stress_full_recent_not_worse": float(full["annualized_return"]) >= REFERENCE_OI9_STRESS,
        "stress_dd_within_30pct": float(full["max_drawdown"]) >= -0.30,
        "validation_positive": float(stress["windows"]["validation"]["annualized_return"]) > 0.0,
        "oos_positive": float(stress["windows"]["oos"]["annualized_return"]) > 0.0,
    }
    independent_improvement["passed"] = bool(
        independent_improvement["stress_full_recent_not_worse"]
        and independent_improvement["stress_dd_within_30pct"]
        and independent_improvement["validation_positive"]
        and independent_improvement["oos_positive"]
    )

    full_index = base_weights.loc[
        pd.Timestamp("2024-08-21"):pd.Timestamp("2026-08-20")
    ].index
    extension_activity = {}
    for product in DCE_EXTENSION_PRODUCTS:
        series = lagged.reindex(index=full_index)[product].fillna(0.0)
        extension_activity[product] = int((series.abs() > 0.0).sum())

    return {
        "role": "lifecycle-defined 13-product 60m OI extension Production L3",
        "production_wiring": False,
        "parameter_search": False,
        "original_supported_products": list(oi9.SUPPORTED_PRODUCTS),
        "extension_products": list(DCE_EXTENSION_PRODUCTS),
        "supported_products": list(EXTENDED_SUPPORTED_PRODUCTS),
        "coverage_gate": coverage,
        "artifact_lineage": {
            "original_prior": 9516473115,
            "original_recent": 9516472727,
            "extension_prior": 9524037509,
            "extension_recent": 9524033190,
        },
        "decision_rule_unchanged": True,
        "candidate": {
            "base": base,
            "stress": stress,
        },
        "economics": economics,
        "independent_improvement_gate": independent_improvement,
        "promotion_gate": _promotion_gate(
            base=base,
            stress=stress,
            economics=economics["stress"],
        ),
        "extension_flow_active_days_full_recent": extension_activity,
        "constraints": {
            "target_gross_leverage_cap": 2.0,
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
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise SystemExit(f"DCE OI extension inputs missing: {missing}")
    report = evaluate(
        specific_raw=pd.read_csv(paths["specific"]),
        base_weights=oi9.load_frozen_weights(paths["weights"]),
        original_60m=oi9.load_60m([paths["prior"], paths["recent"]]),
        extension_60m=oi9.load_60m([paths["ext_prior"], paths["ext_recent"]]),
    )
    output = runtime / "stress80_60m_oi_dce_extension_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
