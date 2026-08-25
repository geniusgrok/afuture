"""Roll-safe float screen for the predeclared product Alpha-efficiency selector."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluate_aggressive_directional as aggressive
import evaluate_return_target_specific as specific

from afuture.alpha_efficiency_policy import AlphaEfficiencyDirectionalPolicy
from afuture.execution_aligned_policy import BASE_COST_BPS

MAX_GROSS_LEVERAGE = 2.0
STRESS_COST_BPS = 15.0
EXTREME_COST_BPS = 30.0
REQUIRED_PRODUCTS = specific.REQUIRED_PRODUCTS
WINDOWS = aggressive.WINDOWS


def build_continuous_price_history(
    raw: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    for column in ("open", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "open", "close"])
    frame = frame[(frame["open"] > 0) & (frame["close"] > 0)]
    frame.drop_duplicates(["date", "product"], keep="last", inplace=True)
    products = sorted({str(item).upper() for item in REQUIRED_PRODUCTS})
    missing = sorted(set(products) - set(frame["product"].unique()))
    if missing:
        raise ValueError(f"continuous signal feed missing products: {missing}")
    open_prices = (
        frame.pivot(index="date", columns="product", values="open")
        .sort_index()
        .reindex(columns=products)
    )
    close = (
        frame.pivot(index="date", columns="product", values="close")
        .sort_index()
        .reindex(index=open_prices.index, columns=open_prices.columns)
    )
    return open_prices, close


def generate_alpha_efficiency_weights(continuous_raw: pd.DataFrame) -> pd.DataFrame:
    open_prices, close = build_continuous_price_history(continuous_raw)
    return AlphaEfficiencyDirectionalPolicy(products=tuple(open_prices.columns)).weight_history(
        open_prices, close
    )


def _window_metrics(series: pd.Series) -> dict[str, dict]:
    return {name: aggressive._window_metrics(series, name) for name in WINDOWS}


def evaluate(specific_raw: pd.DataFrame, continuous_raw: pd.DataFrame) -> dict:
    close_ret, gap_ret, intraday_ret, selections, quality = (
        specific.build_roll_safe_execution_returns(specific_raw)
    )
    weights = generate_alpha_efficiency_weights(continuous_raw)
    weights = weights.reindex(
        index=close_ret.index,
        columns=close_ret.columns,
        fill_value=0.0,
    ).fillna(0.0)

    execution_paths: dict[str, dict] = {}
    turnover: dict[str, float] = {}
    for label, cost in (
        ("base", BASE_COST_BPS),
        ("stress", STRESS_COST_BPS),
        ("extreme", EXTREME_COST_BPS),
    ):
        stream = specific.apply_next_open_product_weights(
            gap_ret,
            intraday_ret,
            weights,
            cost_bps=cost,
        )
        execution_paths[label] = _window_metrics(stream)
        full = weights.loc[pd.Timestamp("2024-08-21") : pd.Timestamp("2026-08-20")]
        turnover[label] = float(full.diff().abs().sum(axis=1).sum())

    report = {
        "source": "frozen continuous completed open/close + concrete next-open execution",
        "role": "selection-biased cheap screen; not Production mechanics",
        "strategy_version": "product_alpha_efficiency_candidate_b",
        "selection_bias_acknowledged": True,
        "selector": {
            "score": "expanding completed product gross Alpha / turnover",
            "lookback": "expanding_no_window_parameter",
            "top_fraction": 0.5,
            "core_share": 0.75,
            "unscored_products": "fail_open",
        },
        "next_open_execution": execution_paths,
        "weight_turnover": turnover,
        "data_quality": quality,
    }
    runtime = Path("runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    selections.to_csv(runtime / "alpha_efficiency_specific_selection.csv", index=False)
    weights.stack().rename("weight").reset_index().query("abs(weight) > 1e-15").to_csv(
        runtime / "alpha_efficiency_weights.csv", index=False
    )
    return report


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    continuous_path = runtime / "broad_daily_universe.csv"
    missing = [str(path) for path in (specific_path, continuous_path) if not path.exists()]
    if missing:
        raise SystemExit(f"alpha-efficiency inputs missing: {missing}")
    report = evaluate(pd.read_csv(specific_path), pd.read_csv(continuous_path))
    output = runtime / "alpha_efficiency_target_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
