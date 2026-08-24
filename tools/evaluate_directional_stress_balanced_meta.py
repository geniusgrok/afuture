"""Fixed-input Production evaluation for the archived 50/50 Base/Stress Meta candidate."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from afuture.directional_stress_balanced_meta import StressBalancedMetaPolicy

import evaluate_aggressive_directional as aggressive
import evaluate_directional_production_mechanics as production
import evaluate_return_target_specific as specific


def _continuous_open_close(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["product"] = frame["product"].astype(str).str.upper()
    for column in ("open", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "open", "close"])
    frame = frame[(frame["open"] > 0.0) & (frame["close"] > 0.0)]
    frame = frame.drop_duplicates(["date", "product"], keep="last")
    products = sorted(frame["product"].unique())
    open_prices = frame.pivot(index="date", columns="product", values="open").sort_index()
    close = frame.pivot(index="date", columns="product", values="close").sort_index()
    return open_prices.reindex(columns=products), close.reindex(index=open_prices.index, columns=products)


def _window_metrics(series: pd.Series) -> dict[str, dict]:
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


def evaluate(specific_raw: pd.DataFrame, continuous_raw: pd.DataFrame) -> dict:
    open_prices, close = _continuous_open_close(continuous_raw)
    policy = StressBalancedMetaPolicy(products=tuple(close.columns))
    weights = policy.weight_history(open_prices, close)

    _close_ret, gap_ret, intraday_ret, _selections, quality = (
        specific.build_roll_safe_execution_returns(specific_raw)
    )
    aligned = weights.reindex(
        index=gap_ret.index,
        columns=gap_ret.columns,
        fill_value=0.0,
    ).fillna(0.0)
    cheap = {}
    for label, cost in (("base", 5.0), ("stress", 15.0)):
        cheap[label] = _window_metrics(
            specific.apply_next_open_product_weights(
                gap_ret,
                intraday_ret,
                aligned,
                cost_bps=cost,
            )
        )

    production_report = production.evaluate_with_weights(specific_raw, weights)
    report = {
        "role": "archived equal Base/Stress Meta re-evaluated under approved Stress-80 gate",
        "parameter_search": False,
        "production_wiring": False,
        "meta": {
            "lookback": policy.meta_lookback,
            "rebalance": policy.meta_rebalance,
            "count": policy.meta_count,
            "score_source": policy.score_source,
            "pool_size": len(policy.template_ids),
            "gross_cap": 2.0,
        },
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
        "legacy_note": "older fixed-history evidence recorded Production Base around 94.59%; current run is authoritative under current mechanics",
    }
    return report


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    continuous_path = runtime / "broad_daily_universe.csv"
    missing = [str(path) for path in (specific_path, continuous_path) if not path.exists()]
    if missing:
        raise SystemExit(f"stress-balanced Meta inputs missing: {missing}")
    report = evaluate(pd.read_csv(specific_path), pd.read_csv(continuous_path))
    output = runtime / "stress_balanced_meta_production_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
