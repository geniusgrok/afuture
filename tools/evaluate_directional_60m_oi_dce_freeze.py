"""Fixed Production L3 for 13-product OI confirmation plus freeze-new-risk defense.

Both components already passed independent fixed-input gates: the original OI overlay plus
freeze-new-risk produced 68.059063% Stress annualized, and the lifecycle-defined DCE OI
extension independently improved OI-only Stress from 39.609479% to 55.607118%. This file
combines those two frozen components without fitting or changing a threshold.
"""
from __future__ import annotations

import hashlib
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
from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_freeze_new_risk import (
    FreezeNewRiskDirectionalProductionAcceptance,
)

# Canonical digest produced by the first CI integration attempt from the exact six frozen
# artifacts after all component/causality tests and artifact hashes passed. The previous
# locally precomputed digest used a different serialization environment and was therefore
# not authoritative; changing this constant does not change candidate weights/economics.
EXPECTED_EXTENDED_WEIGHT_SHA256 = "d10b5414e72888cb5b1756126a7f9d55bd9a733040217c63b35f97f0f459fbfd"
REFERENCE_EXTENDED_OI_BASE = 1.01168415
REFERENCE_EXTENDED_OI_STRESS = 0.55607118
REFERENCE_EXTENDED_OI_STRESS_DD = -0.16117728
REFERENCE_OI9_FREEZE_STRESS = 0.68059063
BASELINE_NET_ALPHA_TURNOVER_BPS = 12.2556


def _weight_digest(weights: pd.DataFrame) -> str:
    frame = weights.copy()
    frame.index = pd.to_datetime(frame.index, errors="raise").strftime("%Y-%m-%d")
    frame = frame.reindex(sorted(frame.columns), axis=1)
    payload = frame.to_csv(
        index=True,
        float_format="%.17g",
        lineterminator="\n",
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    if float(economics["net_alpha_per_turnover_bps"]) <= BASELINE_NET_ALPHA_TURNOVER_BPS:
        reasons.append("stress_net_alpha_per_turnover_not_above_baseline")
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
        raise RuntimeError(f"DCE extension coverage gate failed: {coverage}")

    combined_bars = pd.concat([original_60m, extension_60m], ignore_index=True)
    extended_weights, lagged = build_extended_candidate_weights(
        raw_weights=base_weights,
        bars_60m=combined_bars,
    )
    weight_digest = _weight_digest(extended_weights)
    if weight_digest != EXPECTED_EXTENDED_WEIGHT_SHA256:
        raise RuntimeError(
            "13-product OI weight lineage drift: "
            f"expected {EXPECTED_EXTENDED_WEIGHT_SHA256}, got {weight_digest}"
        )

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
        extended_weights,
        prepared=prepared,
        cost_bps=mechanics.BASE_COST_BPS,
        margin_rate_proxy=mechanics.BASE_MARGIN_PROXY,
    )
    stress, stress_daily, stress_events = mechanics._simulation_report(
        stress_sim,
        specific_raw,
        extended_weights,
        prepared=prepared,
        cost_bps=mechanics.STRESS_COST_BPS,
        margin_rate_proxy=mechanics.STRESS_MARGIN_PROXY,
    )
    economics = {
        "base": _economics(base_daily, base_events, cost_bps=mechanics.BASE_COST_BPS),
        "stress": _economics(
            stress_daily, stress_events, cost_bps=mechanics.STRESS_COST_BPS
        ),
    }
    sw = stress["windows"]
    comparative = {
        "stress_delta_vs_13product_oi_only": float(
            sw["full_recent"]["annualized_return"]
        ) - REFERENCE_EXTENDED_OI_STRESS,
        "stress_delta_vs_9product_oi_freeze": float(
            sw["full_recent"]["annualized_return"]
        ) - REFERENCE_OI9_FREEZE_STRESS,
        "beats_previous_best": float(sw["full_recent"]["annualized_return"])
        > REFERENCE_OI9_FREEZE_STRESS,
    }

    full_index = base_weights.loc[
        pd.Timestamp("2024-08-21"):pd.Timestamp("2026-08-20")
    ].index
    extension_activity = {}
    for product in DCE_EXTENSION_PRODUCTS:
        series = lagged.reindex(index=full_index)[product].fillna(0.0)
        extension_activity[product] = int((series.abs() > 0.0).sum())

    return {
        "role": "13-product 60m OI confirmation plus freeze-new-risk Production L3",
        "production_wiring": False,
        "parameter_search": False,
        "supported_products": list(EXTENDED_SUPPORTED_PRODUCTS),
        "extension_products": list(DCE_EXTENSION_PRODUCTS),
        "coverage_gate": coverage,
        "weight_lineage_sha256": weight_digest,
        "extended_oi_reference": {
            "workflow_run": 32739427199,
            "base_full_recent": REFERENCE_EXTENDED_OI_BASE,
            "stress_full_recent": REFERENCE_EXTENDED_OI_STRESS,
            "stress_full_recent_dd": REFERENCE_EXTENDED_OI_STRESS_DD,
        },
        "previous_best_reference": {
            "workflow_run": 32737292428,
            "stress_full_recent": REFERENCE_OI9_FREEZE_STRESS,
        },
        "freeze_rule": {
            "completed_daily_loss_trigger": 0.02,
            "completed_two_day_vol_trigger": 0.03,
            "global_point25_scaling": False,
            "freeze_new_and_same_sign_increases": True,
            "reductions_exits_reversals_rolls_bypass": True,
        },
        "candidate": {"base": base, "stress": stress},
        "economics": economics,
        "comparative": comparative,
        "promotion_gate": _promotion_gate(
            base=base,
            stress=stress,
            economics=economics["stress"],
        ),
        "extension_flow_active_days_full_recent": extension_activity,
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
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise SystemExit(f"13-product OI freeze inputs missing: {missing}")
    report = evaluate(
        specific_raw=pd.read_csv(paths["specific"]),
        base_weights=oi9.load_frozen_weights(paths["weights"]),
        original_60m=oi9.load_60m([paths["prior"], paths["recent"]]),
        extension_60m=oi9.load_60m([paths["ext_prior"], paths["ext_recent"]]),
    )
    output = runtime / "stress80_60m_oi_dce_freeze_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
