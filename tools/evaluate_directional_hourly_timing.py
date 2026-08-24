"""Fixed-input Production L3 for the parameter-free 60m timing overlay."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from afuture.directional_hourly_timing import (
    apply_hourly_timing_overlay,
    build_completed_hourly_state,
    shift_completed_state_to_next_session,
)

import evaluate_aggressive_directional as aggressive
import evaluate_directional_production_mechanics as production
import evaluate_return_target_specific as specific

USABLE_PRODUCTS = ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")


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
    return (
        data.pivot_table(index="date", columns="product", values="weight", aggfunc="last")
        .sort_index()
        .fillna(0.0)
    )


def _window_metrics(series: pd.Series) -> dict[str, dict]:
    return {name: aggressive._window_metrics(series, name) for name in aggressive.WINDOWS}


def _economics(attribution: dict) -> dict[str, float]:
    gross = float(attribution["alpha"]["gross_signal_pnl"])
    turnover = float(attribution["transaction_cost"]["turnover_notional"])
    cost = float(attribution["transaction_cost"]["total_cost"])
    net = gross - cost
    return {
        "gross_signal_pnl": gross,
        "turnover_notional": turnover,
        "transaction_cost": cost,
        "net_alpha_pnl": net,
        "net_alpha_per_turnover_bps": net / turnover * 10000.0 if turnover > 0.0 else 0.0,
    }


def _promotion_gate(report: dict) -> dict:
    base = report["base"]["windows"]
    stress = report["stress"]["windows"]
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
    hourly_prior: pd.DataFrame,
    hourly_recent: pd.DataFrame,
) -> dict:
    baseline = _wide_weights(baseline_weights_raw)
    calendar = pd.DatetimeIndex(sorted(baseline.index.unique()))
    hourly = pd.concat([hourly_prior, hourly_recent], ignore_index=True)
    hourly["product"] = hourly["product"].astype(str).str.upper()
    hourly = hourly[hourly["product"].isin(USABLE_PRODUCTS)].copy()
    completed = build_completed_hourly_state(hourly, trading_days=calendar)
    target_state = shift_completed_state_to_next_session(completed, trading_days=calendar)
    candidate = apply_hourly_timing_overlay(baseline, target_state)

    _close_ret, gap_ret, intraday_ret, _selections, quality = specific.build_roll_safe_execution_returns(specific_raw)
    aligned = candidate.reindex(index=gap_ret.index, columns=gap_ret.columns, fill_value=0.0).fillna(0.0)
    cheap = {}
    for label, cost in (("base", 5.0), ("stress", 15.0)):
        cheap[label] = _window_metrics(
            specific.apply_next_open_product_weights(gap_ret, intraday_ret, aligned, cost_bps=cost)
        )

    prod = production.evaluate_with_weights(specific_raw, candidate)
    raw_turnover = float(baseline.diff().abs().sum(axis=1).sum())
    candidate_turnover = float(candidate.diff().abs().sum(axis=1).sum())
    changed_cells = int(((candidate - baseline).abs() > 1e-12).sum().sum())
    report = {
        "role": "parameter-free completed 60m Price x OI confirmation for Production risk increases",
        "parameter_search": False,
        "production_wiring": False,
        "usable_products": list(USABLE_PRODUCTS),
        "hourly_rule": "classic Price x OI quadrant; completed D state applies only to D+1 risk increases",
        "missing_evidence": "preserve baseline target",
        "bypass": ["reduction", "exit", "reversal"],
        "baseline_weight_turnover": raw_turnover,
        "candidate_weight_turnover": candidate_turnover,
        "changed_weight_cells": changed_cells,
        "cheap_screen": cheap,
        "specific_quality": quality,
        "production_l3": {key: value for key, value in prod.items() if not key.startswith("_")},
        "economics": {
            "base": _economics(prod["production_attribution"]["base"]),
            "stress": _economics(prod["production_attribution"]["stress"]),
        },
        "promotion_gate": _promotion_gate(prod),
    }
    return report


def main() -> None:
    runtime = Path("runtime")
    paths = {
        "specific": runtime / "return_target_specific_contracts.csv",
        "weights": runtime / "execution_aligned_weights.csv",
        "prior": runtime / "prior_two_year_broad_60m.csv",
        "recent": runtime / "two_year_broad_60m.csv",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise SystemExit(f"hourly timing inputs missing: {missing}")
    report = evaluate(
        pd.read_csv(paths["specific"]),
        pd.read_csv(paths["weights"]),
        pd.read_csv(paths["prior"]),
        pd.read_csv(paths["recent"]),
    )
    output = runtime / "hourly_timing_production_report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
