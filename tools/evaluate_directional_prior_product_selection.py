"""Prior-only frozen product selection under the approved Stress-80 research gate.

Selection uses only pre-Production prior1/prior2 Stress 15bp product economics from the
validated current weight lineage. A product survives only if its net alpha per turnover is
strictly positive in both prior windows. The resulting set is frozen from 2024-08-21
forward. No top-k, rank cut, threshold search, or 2024-2026 outcome enters selection.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from afuture.directional_prior_product_selection import (
    freeze_positive_prior_products,
    redistribute_to_frozen_products,
)

import evaluate_aggressive_directional as aggressive
import evaluate_directional_production_mechanics as production
import evaluate_return_target_specific as specific

STRESS_COST_BPS = 15.0
EFFECTIVE_DATE = pd.Timestamp("2024-08-21")
PRIOR_WINDOWS = {
    "prior1": (pd.Timestamp("2022-08-22"), pd.Timestamp("2023-08-20")),
    "prior2": (pd.Timestamp("2023-08-21"), pd.Timestamp("2024-08-20")),
}


def load_frozen_weights(path: Path) -> pd.DataFrame:
    rows = pd.read_csv(path)
    required = {"level_0", "level_1", "weight"}
    if not required.issubset(rows.columns):
        raise ValueError(
            f"frozen weight artifact missing columns: {sorted(required - set(rows.columns))}"
        )
    rows["level_0"] = pd.to_datetime(rows["level_0"], errors="coerce").dt.normalize()
    rows = rows[rows["level_0"].notna()].copy()
    rows["level_1"] = rows["level_1"].astype(str).str.upper()
    rows["weight"] = pd.to_numeric(rows["weight"], errors="coerce").fillna(0.0)
    if rows.duplicated(["level_0", "level_1"]).any():
        raise ValueError("frozen execution-aligned weights contain duplicates")
    frame = rows.pivot(index="level_0", columns="level_1", values="weight").fillna(0.0)
    frame.index.name = None
    frame.columns.name = None
    gross = frame.abs().sum(axis=1)
    if bool((gross > 2.0 + 1e-10).any()):
        raise ValueError("frozen execution-aligned weights exceed 2x gross")
    return frame.sort_index()


def _product_economics(
    *,
    weights: pd.DataFrame,
    gap_returns: pd.DataFrame,
    intraday_returns: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost_bps: float,
) -> dict[str, dict[str, float]]:
    aligned = weights.reindex(
        index=gap_returns.index,
        columns=gap_returns.columns,
        fill_value=0.0,
    ).fillna(0.0)
    old = aligned.shift(1).fillna(0.0)
    gaps = gap_returns.reindex_like(aligned).fillna(0.0)
    intraday = intraday_returns.reindex_like(aligned).fillna(0.0)
    gross = old * gaps + aligned * intraday
    turnover = aligned.diff().abs()
    if len(turnover):
        turnover.iloc[0] = aligned.iloc[0].abs()

    gross = gross.loc[start:end]
    turnover = turnover.loc[start:end]
    report: dict[str, dict[str, float]] = {}
    for product in aligned.columns:
        product_gross = float(gross[product].sum())
        product_turnover = float(turnover[product].sum())
        transaction_cost = product_turnover * float(cost_bps) / 10000.0
        net = product_gross - transaction_cost
        ratio = (
            net / product_turnover * 10000.0
            if product_turnover > 1e-15
            else 0.0
        )
        report[str(product)] = {
            "gross_return_sum": product_gross,
            "turnover_weight": product_turnover,
            "transaction_cost_return": transaction_cost,
            "net_alpha_return": net,
            "net_alpha_per_turnover_bps": ratio,
        }
    return report


def _metrics(series: pd.Series) -> dict[str, dict]:
    return {
        name: aggressive._window_metrics(series, name)
        for name in aggressive.WINDOWS
    }


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
    *,
    specific_raw: pd.DataFrame,
    frozen_weights: pd.DataFrame,
) -> dict:
    _close_returns, gap_returns, intraday_returns, _selections, quality = (
        specific.build_roll_safe_execution_returns(specific_raw)
    )
    prior_evidence = {
        name: _product_economics(
            weights=frozen_weights,
            gap_returns=gap_returns,
            intraday_returns=intraday_returns,
            start=start,
            end=end,
            cost_bps=STRESS_COST_BPS,
        )
        for name, (start, end) in PRIOR_WINDOWS.items()
    }
    selected = freeze_positive_prior_products(
        prior1=prior_evidence["prior1"],
        prior2=prior_evidence["prior2"],
    )

    candidate = frozen_weights.copy()
    post = candidate.loc[EFFECTIVE_DATE:].copy()
    candidate.loc[EFFECTIVE_DATE:] = redistribute_to_frozen_products(
        post,
        frozen_products=selected,
    )

    aligned = candidate.reindex(
        index=gap_returns.index,
        columns=gap_returns.columns,
        fill_value=0.0,
    ).fillna(0.0)
    cheap = {}
    for label, cost in (("base", 5.0), ("stress", 15.0)):
        cheap[label] = _metrics(
            specific.apply_next_open_product_weights(
                gap_returns,
                intraday_returns,
                aligned,
                cost_bps=cost,
            )
        )

    production_report = production.evaluate_with_weights(specific_raw, candidate)
    return {
        "role": "prior-only frozen product selection for Production capital concentration",
        "parameter_search": False,
        "production_wiring": False,
        "selection": {
            "evidence_windows": ["prior1", "prior2"],
            "cost_bps": STRESS_COST_BPS,
            "criterion": "strictly positive net alpha per turnover in both prior windows",
            "threshold_search": False,
            "rank_or_top_k": False,
            "effective_date": EFFECTIVE_DATE.date().isoformat(),
            "selected_products": list(selected),
            "selected_count": len(selected),
            "universe_count": int(len(frozen_weights.columns)),
        },
        "prior_evidence": prior_evidence,
        "cheap_screen": cheap,
        "specific_quality": quality,
        "production_l3": {
            key: value
            for key, value in production_report.items()
            if not key.startswith("_")
        },
        "economics": {
            "base": _economics(production_report["production_attribution"]["base"]),
            "stress": _economics(production_report["production_attribution"]["stress"]),
        },
        "promotion_gate": _promotion_gate(production_report),
    }


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    weights_path = runtime / "execution_aligned_weights.csv"
    missing = [str(path) for path in (specific_path, weights_path) if not path.exists()]
    if missing:
        raise SystemExit(f"prior-product selection inputs missing: {missing}")
    report = evaluate(
        specific_raw=pd.read_csv(specific_path),
        frozen_weights=load_frozen_weights(weights_path),
    )
    output = runtime / "prior_product_selection_production_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
