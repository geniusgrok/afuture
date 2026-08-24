"""Fixed-input Production gate for the frozen 9-product 60m Price x OI overlay.

This evaluator never fits a parameter. It reuses the validated execution-aligned weight
artifact, the fixed concrete-contract daily artifact, and the official four-year 60m
coverage artifacts. Before interpreting the candidate it reproduces both the exact cheap
specific-contract baseline and the validated Production baseline.
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

from afuture.directional_60m_oi_confirmation import (
    SUPPORTED_PRODUCTS,
    apply_oi_confirmation_to_weights,
    build_daily_price_oi_flow,
    lag_flow_to_target_days,
)

import evaluate_aggressive_directional as aggressive
import evaluate_directional_production_mechanics as mechanics
import evaluate_return_target_specific as specific

BASE_COST_BPS = 5.0
STRESS_COST_BPS = 15.0
EXPECTED_CHEAP_BASE = 1.872603
EXPECTED_CHEAP_STRESS = 1.093145
EXPECTED_PRODUCTION_BASE = 1.090636
EXPECTED_PRODUCTION_STRESS = 0.289559
BASELINE_TOLERANCE = 0.005


def load_frozen_weights(path: Path) -> pd.DataFrame:
    rows = pd.read_csv(path)
    required = {"level_0", "level_1", "weight"}
    missing = required - set(rows.columns)
    if missing:
        raise ValueError(f"frozen weights missing columns: {sorted(missing)}")
    rows["level_0"] = pd.to_datetime(rows["level_0"], errors="coerce").dt.normalize()
    rows["level_1"] = rows["level_1"].astype(str).str.upper()
    rows["weight"] = pd.to_numeric(rows["weight"], errors="coerce")
    rows = rows.dropna(subset=["level_0", "level_1", "weight"])
    if rows.duplicated(["level_0", "level_1"]).any():
        raise ValueError("frozen weights contain duplicate date/product rows")
    weights = (
        rows.pivot(index="level_0", columns="level_1", values="weight")
        .sort_index()
        .fillna(0.0)
    )
    weights.index.name = None
    weights.columns.name = None
    if bool((weights.abs().sum(axis=1) > 2.0 + 1e-10).any()):
        raise ValueError("frozen weights exceed 2x gross")
    return weights.astype(float)


def load_60m(paths: list[Path]) -> pd.DataFrame:
    frames = [pd.read_csv(path) for path in paths]
    frame = pd.concat(frames, ignore_index=True)
    required = {"datetime", "product", "symbol", "open", "close", "volume", "hold"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"60m artifact missing columns: {sorted(missing)}")
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame = frame.dropna(subset=["datetime", "product", "symbol"])
    frame = frame.drop_duplicates(["datetime", "product", "symbol"], keep="last")
    return frame.sort_values(["datetime", "product", "symbol"]).reset_index(drop=True)


def build_candidate_weights(base_weights: pd.DataFrame, bars_60m: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    flow = build_daily_price_oi_flow(bars_60m)
    lagged = lag_flow_to_target_days(
        flow,
        target_days=base_weights.index,
        products=base_weights.columns,
    )
    candidate = apply_oi_confirmation_to_weights(
        raw_weights=base_weights,
        confirming_flow=lagged,
        supported_products=SUPPORTED_PRODUCTS,
    )
    candidate = candidate.reindex(index=base_weights.index, columns=base_weights.columns).fillna(0.0)
    if bool((candidate.abs().sum(axis=1) > base_weights.abs().sum(axis=1) + 1e-10).any()):
        raise AssertionError("60m OI candidate increased baseline gross")
    return candidate, lagged


def _cheap_report(specific_raw: pd.DataFrame, weights: pd.DataFrame) -> dict:
    _close, gap, intraday, _selections, _quality = specific.build_roll_safe_execution_returns(specific_raw)
    weights = weights.reindex(index=gap.index, columns=gap.columns, fill_value=0.0).fillna(0.0)
    result = {}
    for label, cost in (("base", BASE_COST_BPS), ("stress", STRESS_COST_BPS)):
        series = specific.apply_next_open_product_weights(
            gap,
            intraday,
            weights,
            cost_bps=cost,
        )
        result[label] = {
            name: aggressive._window_metrics(series, name)
            for name in aggressive.WINDOWS
        }
    turnover = weights.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = float(weights.iloc[0].abs().sum())
    result["weight_turnover_full_path"] = float(turnover.sum())
    return result


def baseline_reproduction_gate(*, cheap: dict, production: dict) -> dict:
    checks = {
        "cheap_base": abs(float(cheap["base"]["full_recent"]["annualized_return"]) - EXPECTED_CHEAP_BASE) <= BASELINE_TOLERANCE,
        "cheap_stress": abs(float(cheap["stress"]["full_recent"]["annualized_return"]) - EXPECTED_CHEAP_STRESS) <= BASELINE_TOLERANCE,
        "production_base": abs(float(production["base"]["windows"]["full_recent"]["annualized_return"]) - EXPECTED_PRODUCTION_BASE) <= BASELINE_TOLERANCE,
        "production_stress": abs(float(production["stress"]["windows"]["full_recent"]["annualized_return"]) - EXPECTED_PRODUCTION_STRESS) <= BASELINE_TOLERANCE,
    }
    return {
        "passed": bool(all(checks.values())),
        "checks": checks,
    }


def production_promotion_gate(*, base: dict, stress: dict) -> dict:
    reasons: list[str] = []
    base_full = base["windows"]["full_recent"]
    stress_full = stress["windows"]["full_recent"]
    if float(base_full["annualized_return"]) < 0.80:
        reasons.append("base_full_recent_below_80pct")
    if float(stress_full["annualized_return"]) < 0.80:
        reasons.append("stress_full_recent_below_80pct")
    if float(stress_full["max_drawdown"]) < -0.30:
        reasons.append("stress_full_recent_dd_above_30pct")
    if bool(stress_full.get("halted", False)):
        reasons.append("stress_full_recent_halted")
    if float(stress_full.get("max_realized_gross_notional_ratio", 0.0)) > 2.0 + 1e-10:
        reasons.append("stress_full_recent_gross_above_2x")
    if int(stress_full.get("margin_reject_days", 0)) > 0:
        reasons.append("stress_full_recent_margin_reject")

    for window in ("validation", "oos"):
        item = stress["windows"][window]
        if float(item["annualized_return"]) <= 0.0:
            reasons.append(f"{window}_stress_not_positive")
        if float(item["max_drawdown"]) < -0.30:
            reasons.append(f"{window}_stress_dd_above_30pct")
        if bool(item.get("halted", False)):
            reasons.append(f"{window}_stress_halted")
        if float(item.get("max_realized_gross_notional_ratio", 0.0)) > 2.0 + 1e-10:
            reasons.append(f"{window}_stress_gross_above_2x")
        if int(item.get("margin_reject_days", 0)) > 0:
            reasons.append(f"{window}_stress_margin_reject")
    return {"passed": not reasons, "reasons": reasons}


def evaluate(
    *,
    specific_raw: pd.DataFrame,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
) -> dict:
    candidate_weights, lagged_flow = build_candidate_weights(base_weights, bars_60m)

    cheap_baseline = _cheap_report(specific_raw, base_weights)
    cheap_candidate = _cheap_report(specific_raw, candidate_weights)
    production_baseline = mechanics.evaluate_with_weights(specific_raw, base_weights)
    baseline_gate = baseline_reproduction_gate(
        cheap=cheap_baseline,
        production=production_baseline,
    )
    if not baseline_gate["passed"]:
        raise RuntimeError(f"60m OI baseline lineage reproduction failed: {baseline_gate}")

    production_candidate = mechanics.evaluate_with_weights(specific_raw, candidate_weights)
    promotion = production_promotion_gate(
        base=production_candidate["base"],
        stress=production_candidate["stress"],
    )

    full_index = base_weights.loc[pd.Timestamp("2024-08-21"):pd.Timestamp("2026-08-20")].index
    supported_activity = {}
    for product in SUPPORTED_PRODUCTS:
        series = lagged_flow.reindex(index=full_index)[product].fillna(0.0)
        supported_activity[product] = int((series.abs() > 0.0).sum())

    return {
        "role": "fixed-input 9-product 60m Price x OI confirmation Production gate",
        "production_wiring": False,
        "parameter_search": False,
        "supported_products": list(SUPPORTED_PRODUCTS),
        "coverage_source": {
            "workflow_run": 32717335780,
            "coverage_artifact": 9516487386,
            "prior_artifact": 9516473115,
            "recent_artifact": 9516472727,
            "rule": "each supported product >=80% exact daily 60m coverage in prior and recent eras; at least two exchanges",
        },
        "decision_rule": "D dominant-contract first-open to last-close direction when D hold_last > hold_first; D+1 only; suppress only new/same-sign increases",
        "baseline_reproduction": baseline_gate,
        "cheap": {
            "baseline": cheap_baseline,
            "candidate": cheap_candidate,
        },
        "production": {
            "baseline": {key: value for key, value in production_baseline.items() if not key.startswith("_")},
            "candidate": {key: value for key, value in production_candidate.items() if not key.startswith("_")},
        },
        "promotion_gate": promotion,
        "supported_flow_active_days_full_recent": supported_activity,
        "constraints": {
            "target_gross_leverage_cap": 2.0,
            "hard_margin_ratio": 0.35,
            "min_available_ratio": 0.25,
            "max_contract_volume": 35,
            "daily_loss_ratio": 0.05,
            "total_drawdown_ratio": 0.30,
            "reduction_first_unchanged": True,
            "risk_manager_authority_unchanged": True,
        },
        "_candidate_weights": candidate_weights,
        "_candidate_base_daily": production_candidate["_base_daily"],
        "_candidate_stress_daily": production_candidate["_stress_daily"],
        "_candidate_base_events": production_candidate["_base_events"],
        "_candidate_stress_events": production_candidate["_stress_events"],
    }


def _jsonable(report: dict) -> dict:
    return {key: value for key, value in report.items() if not key.startswith("_")}


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    weights_path = runtime / "execution_aligned_weights.csv"
    prior_60m = runtime / "prior_two_year_broad_60m.csv"
    recent_60m = runtime / "two_year_broad_60m.csv"
    required = [specific_path, weights_path, prior_60m, recent_60m]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit(f"60m OI Production inputs missing: {missing}")

    report = evaluate(
        specific_raw=pd.read_csv(specific_path),
        base_weights=load_frozen_weights(weights_path),
        bars_60m=load_60m([prior_60m, recent_60m]),
    )
    report["_candidate_weights"].stack().rename("weight").reset_index().query(
        "abs(weight) > 1e-15"
    ).to_csv(runtime / "stress80_60m_oi_weights.csv", index=False)
    report["_candidate_base_daily"].to_csv(runtime / "stress80_60m_oi_base_daily.csv", index=False)
    report["_candidate_stress_daily"].to_csv(runtime / "stress80_60m_oi_stress_daily.csv", index=False)
    report["_candidate_base_events"].to_csv(runtime / "stress80_60m_oi_base_events.csv", index=False)
    report["_candidate_stress_events"].to_csv(runtime / "stress80_60m_oi_stress_events.csv", index=False)
    output = runtime / "stress80_60m_oi_production_report.json"
    output.write_text(
        json.dumps(_jsonable(report), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
