"""Final specific-contract L4 for the frozen execution-aligned directional policy."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluate_aggressive_directional as aggressive
import evaluate_return_target_specific as specific

from afuture.execution_aligned_policy import (
    _EXECUTION_TEMPLATE_IDS,
    BASE_COST_BPS,
    META_COUNT,
    META_LOOKBACK,
    META_REBALANCE,
    META_SCORE_SOURCE,
    ExecutionAlignedAggressivePolicy,
)

MAX_GROSS_LEVERAGE = 2.0
STRESS_COST_BPS = 15.0
EXTREME_COST_BPS = 30.0
PRISTINE_FINAL_OOS = False
REQUIRED_PRODUCTS = specific.REQUIRED_PRODUCTS
WINDOWS = aggressive.WINDOWS

EXECUTION_SELECTION = {
    "selection_bias": "full_recent_specific_execution_template_rank_fit",
    "pool_size": len(_EXECUTION_TEMPLATE_IDS),
    "pool_ids": list(_EXECUTION_TEMPLATE_IDS),
    "meta_lookback": META_LOOKBACK,
    "rebalance": META_REBALANCE,
    "count": META_COUNT,
    "effective_gross_leverage": MAX_GROSS_LEVERAGE,
    "meta_score_source": META_SCORE_SOURCE,
    "execution_timing": "prior_close_signal_next_session_open_rebalance",
}


def _product_order(products) -> list[str]:
    return sorted({str(item).upper() for item in products})


def build_continuous_execution_proxy(
    raw: pd.DataFrame,
    *,
    products: tuple[str, ...] = REQUIRED_PRODUCTS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    for column in ("open", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "open", "close"])
    frame = frame[(frame["open"] > 0) & (frame["close"] > 0)]
    frame.drop_duplicates(["date", "product"], keep="last", inplace=True)
    ordered_products = _product_order(products)
    open_prices = (
        frame.pivot(index="date", columns="product", values="open")
        .sort_index()
        .reindex(columns=ordered_products)
    )
    close = (
        frame.pivot(index="date", columns="product", values="close")
        .sort_index()
        .reindex(index=open_prices.index, columns=open_prices.columns)
    )
    gap = open_prices.div(close.shift(1)) - 1.0
    intraday = close.div(open_prices) - 1.0
    gap = gap.mask(gap.abs() > aggressive.MAX_ABS_DAILY_RETURN)
    intraday = intraday.mask(intraday.abs() > aggressive.MAX_ABS_DAILY_RETURN)
    return gap, intraday


def _template_lookup() -> dict[str, aggressive.DirectionalTemplate]:
    lookup = {
        aggressive.template_id(template): template
        for template in aggressive.directional_templates()
    }
    missing = [item for item in _EXECUTION_TEMPLATE_IDS if item not in lookup]
    if missing:
        raise ValueError(f"execution-aligned templates missing: {missing}")
    return lookup


def _meta_weight_path(
    score_streams: dict[str, pd.Series],
    template_weights: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    frame = pd.DataFrame(score_streams).sort_index().fillna(0.0)
    names = list(frame.columns)
    scores = aggressive._trailing_score_matrix(frame, META_LOOKBACK)
    example = template_weights[names[0]].reindex(frame.index).fillna(0.0)
    final = pd.DataFrame(0.0, index=frame.index, columns=example.columns)
    selected: list[int] = []
    for position, timestamp in enumerate(frame.index):
        if position >= META_LOOKBACK and (not selected or position % META_REBALANCE == 0):
            row = scores[position]
            valid = np.flatnonzero(np.isfinite(row))
            selected = (
                [int(item) for item in valid[np.argsort(-row[valid], kind="stable")][:META_COUNT]]
                if valid.size
                else []
            )
        if selected:
            rows = [template_weights[names[item]].loc[timestamp] for item in selected]
            final.loc[timestamp] = pd.concat(rows, axis=1).mean(axis=1)
    gross = final.abs().sum(axis=1)
    if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
        raise AssertionError("execution-aligned meta path exceeds 2x gross")
    return final


def generate_execution_signal_weights(
    continuous_raw: pd.DataFrame,
) -> pd.DataFrame:
    """Generate the exact causal weight path used by the production policy.

    The L4 evaluator previously rebuilt the meta allocator through a research helper,
    which could drift from production scoring constants. Keep data normalization here,
    but delegate all signal/template/meta behavior to the production policy itself.
    """
    frame = continuous_raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    for column in ("open", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "open", "close"])
    frame = frame[(frame["open"] > 0) & (frame["close"] > 0)]
    frame.drop_duplicates(["date", "product"], keep="last", inplace=True)

    ordered_products = _product_order(REQUIRED_PRODUCTS)
    available = set(frame["product"].unique())
    missing = sorted(set(ordered_products) - available)
    if missing:
        raise ValueError(f"continuous signal feed missing products: {missing}")

    open_prices = (
        frame.pivot(index="date", columns="product", values="open")
        .sort_index()
        .reindex(columns=ordered_products)
    )
    close = (
        frame.pivot(index="date", columns="product", values="close")
        .sort_index()
        .reindex(index=open_prices.index, columns=open_prices.columns)
    )
    return ExecutionAlignedAggressivePolicy(products=tuple(ordered_products)).weight_history(
        open_prices, close
    )


def _window_metrics(series: pd.Series) -> dict[str, dict]:
    return {name: aggressive._window_metrics(series, name) for name in WINDOWS}


def evaluate(
    specific_raw: pd.DataFrame,
    continuous_raw: pd.DataFrame,
) -> dict:
    close_ret, gap_ret, intraday_ret, selections, quality = (
        specific.build_roll_safe_execution_returns(specific_raw)
    )
    weights = generate_execution_signal_weights(continuous_raw)
    weights = weights.reindex(
        index=close_ret.index,
        columns=close_ret.columns,
        fill_value=0.0,
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
            specific.apply_next_open_product_weights(
                gap_ret,
                intraday_ret,
                weights,
                cost_bps=cost,
            )
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
            "specific-ranked intraday-meta roll-safe path does not retain >=100% annualized return with base drawdown <30% and positive stress return"
        )

    report = {
        "source": "AKShare/Sina continuous OHLC signals + concrete daily contract execution",
        "role": "selection-biased specific-ranked roll-safe L4 for aggressive directional production",
        "specific_contracts": True,
        "roll_safe": True,
        "selection_frozen": True,
        "selection_bias_acknowledged": True,
        "product_ordering": "alphabetical",
        "pristine_final_oos": PRISTINE_FINAL_OOS,
        "historical_l1_available": False,
        "max_gross_leverage": MAX_GROSS_LEVERAGE,
        "execution_selection": EXECUTION_SELECTION,
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
    selections.to_csv(output / "execution_aligned_specific_selection.csv", index=False)
    weights.stack().rename("weight").reset_index().query("abs(weight) > 1e-15").to_csv(
        output / "execution_aligned_weights.csv", index=False
    )
    return report


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    continuous_path = runtime / "broad_daily_universe.csv"
    missing = [str(path) for path in (specific_path, continuous_path) if not path.exists()]
    if missing:
        raise SystemExit(f"execution-aligned L4 inputs missing: {missing}")
    report = evaluate(
        pd.read_csv(specific_path),
        pd.read_csv(continuous_path),
    )
    output = runtime / "execution_aligned_target_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "execution_selection": report["execution_selection"],
                "base_full_recent": report["next_open_execution"]["base"]["full_recent"],
                "stress_full_recent": report["next_open_execution"]["stress"]["full_recent"],
                "base_oos": report["next_open_execution"]["base"]["oos"],
                "stress_oos": report["next_open_execution"]["stress"]["oos"],
                "target": report["target"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
