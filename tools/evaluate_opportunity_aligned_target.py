"""Selection-biased roll-safe L4 for opportunity-driven directional V2."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from afuture.execution_aligned_runtime import FROZEN_PRODUCTS
from afuture.opportunity_aligned_policy import OpportunityAlignedAggressivePolicy

MAX_GROSS_LEVERAGE = 2.0
BASE_COST_BPS = 5.0
STRESS_COST_BPS = 15.0
EXTREME_COST_BPS = 30.0
PRISTINE_FINAL_OOS = False
REQUIRED_PRODUCTS = FROZEN_PRODUCTS


def _product_order(products) -> list[str]:
    return sorted({str(item).upper() for item in products})


def build_continuous_signal_history(
    raw: pd.DataFrame,
    *,
    products: tuple[str, ...] = REQUIRED_PRODUCTS,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame | None, pd.DataFrame | None]:
    frame = raw.copy()
    required_columns = {"date", "product", "open", "close"}
    if not required_columns.issubset(frame.columns):
        missing = sorted(required_columns - set(frame.columns))
        raise ValueError(f"continuous signal feed missing columns: {missing}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    numeric = [column for column in ("open", "close", "volume", "hold") if column in frame]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "open", "close"])
    frame = frame[(frame["open"] > 0) & (frame["close"] > 0)]
    frame.drop_duplicates(["date", "product"], keep="last", inplace=True)

    ordered = _product_order(products)
    available = set(frame["product"].unique())
    missing_products = sorted(set(ordered) - available)
    if missing_products:
        raise ValueError(f"continuous signal feed missing products: {missing_products}")

    def pivot(column: str) -> pd.DataFrame:
        return (
            frame.pivot(index="date", columns="product", values=column)
            .sort_index()
            .reindex(columns=ordered)
        )

    open_prices = pivot("open")
    close = pivot("close").reindex(index=open_prices.index, columns=open_prices.columns)
    volume = None
    open_interest = None
    if "volume" in frame.columns and "hold" in frame.columns:
        volume = pivot("volume").reindex(index=open_prices.index, columns=open_prices.columns)
        open_interest = pivot("hold").reindex(index=open_prices.index, columns=open_prices.columns)
    return open_prices, close, volume, open_interest


def generate_execution_signal_weights(continuous_raw: pd.DataFrame) -> pd.DataFrame:
    open_prices, close, volume, open_interest = build_continuous_signal_history(
        continuous_raw, products=REQUIRED_PRODUCTS
    )
    products = tuple(open_prices.columns)
    return OpportunityAlignedAggressivePolicy(products=products).weight_history(
        open_prices,
        close,
        volume=volume,
        open_interest=open_interest,
    )


def _window_metrics(series: pd.Series) -> dict[str, dict]:
    import evaluate_aggressive_directional as aggressive

    return {name: aggressive._window_metrics(series, name) for name in aggressive.WINDOWS}


def evaluate(specific_raw: pd.DataFrame, continuous_raw: pd.DataFrame) -> dict:
    import evaluate_return_target_specific as specific

    close_ret, gap_ret, intraday_ret, selections, quality = (
        specific.build_roll_safe_execution_returns(specific_raw)
    )
    weights = generate_execution_signal_weights(continuous_raw)
    weights = weights.reindex(
        index=close_ret.index, columns=close_ret.columns, fill_value=0.0
    ).fillna(0.0)

    close_paths: dict[str, dict] = {}
    execution_paths: dict[str, dict] = {}
    for label, cost in (
        ("base", BASE_COST_BPS),
        ("stress", STRESS_COST_BPS),
        ("extreme", EXTREME_COST_BPS),
    ):
        close_paths[label] = _window_metrics(
            specific.apply_product_weights(close_ret, weights, cost_bps=cost)
        )
        execution_paths[label] = _window_metrics(
            specific.apply_next_open_product_weights(gap_ret, intraday_ret, weights, cost_bps=cost)
        )

    quality_reasons = [
        f"{product} missing same-contract next return >=5%"
        for product, item in sorted(quality.items())
        if item["missing_next_ratio"] >= 0.05
    ]
    base_recent = execution_paths["base"]["full_recent"]
    stress_recent = execution_paths["stress"]["full_recent"]
    target_pass = bool(
        base_recent["annualized_return"] >= 1.0
        and base_recent["max_drawdown"] > -0.30
        and base_recent["active_days"] >= 50
        and stress_recent["annualized_return"] > 0.0
    )
    reasons = list(quality_reasons)
    if not target_pass:
        reasons.append(
            "opportunity-driven next-open path does not retain >=100% Base annualized return, Base drawdown <30%, and positive Stress"
        )

    report = {
        "source": "continuous completed OHLCV/OI signals + concrete daily contract execution",
        "role": "selection-biased opportunity-driven roll-safe L4 candidate",
        "strategy_version": "opportunity_directional_v2",
        "specific_contracts": True,
        "roll_safe": True,
        "selection_bias_acknowledged": True,
        "pristine_final_oos": PRISTINE_FINAL_OOS,
        "max_gross_leverage": MAX_GROSS_LEVERAGE,
        "opportunity": {
            "core_share": 0.75,
            "active_selection": "top_half",
            "factor_count": 6,
            "activity_required_for_overlay": True,
        },
        "data_quality": quality,
        "close_to_close_diagnostic": close_paths,
        "next_open_execution": execution_paths,
        "target": {
            "annualized_return": 1.0,
            "max_drawdown": -0.30,
            "stress_must_be_positive": True,
            "gross_leverage_cap": MAX_GROSS_LEVERAGE,
            "target_met": bool(target_pass and not quality_reasons),
            "reasons": reasons,
        },
    }
    output = Path("runtime")
    output.mkdir(parents=True, exist_ok=True)
    selections.to_csv(output / "opportunity_directional_specific_selection.csv", index=False)
    weights.stack().rename("weight").reset_index().query("abs(weight) > 1e-15").to_csv(
        output / "opportunity_directional_weights.csv", index=False
    )
    return report


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    continuous_path = runtime / "broad_daily_universe.csv"
    missing = [str(path) for path in (specific_path, continuous_path) if not path.exists()]
    if missing:
        raise SystemExit(f"opportunity directional L4 inputs missing: {missing}")
    report = evaluate(pd.read_csv(specific_path), pd.read_csv(continuous_path))
    output = runtime / "opportunity_directional_target_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
