"""Causal product selection from the directional strategy's own realized Alpha efficiency."""

from __future__ import annotations

from math import ceil

import pandas as pd

MAX_ABS_DAILY_RETURN = 0.20
ALPHA_EFFICIENCY_CORE_SHARE = 0.75


def _aligned_prices(
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
    raw_weights: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    index = raw_weights.index
    columns = raw_weights.columns
    open_aligned = open_prices.reindex(index=index, columns=columns).astype(float)
    close_aligned = close.reindex(index=index, columns=columns).astype(float)
    raw = raw_weights.reindex(index=index, columns=columns).fillna(0.0).astype(float)
    return open_aligned, close_aligned, raw


def build_product_alpha_efficiency_score(
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
    raw_weights: pd.DataFrame,
) -> pd.DataFrame:
    """Rankable expanding gross Alpha per unit turnover using completed outcomes only.

    The score at row ``t`` contains cumulative product-level Alpha and turnover through
    ``t-1``.  With a common flat bps transaction-cost endpoint, ranking by net Alpha per
    turnover is identical because the endpoint subtracts the same constant from every
    product's gross-Alpha/turnover ratio.
    """
    open_aligned, close_aligned, raw = _aligned_prices(
        open_prices,
        close,
        raw_weights,
    )
    intraday = close_aligned.div(open_aligned) - 1.0
    intraday = intraday.mask(intraday.abs() > MAX_ABS_DAILY_RETURN)
    gross_alpha = raw * intraday.fillna(0.0)
    turnover = raw.diff().abs()
    if len(turnover):
        turnover.iloc[0] = raw.iloc[0].abs()

    completed_alpha = gross_alpha.cumsum().shift(1)
    completed_turnover = turnover.cumsum().shift(1)
    return completed_alpha.div(completed_turnover.where(completed_turnover > 1e-12))


def apply_alpha_efficiency_overlay(
    raw_weights: pd.DataFrame,
    score: pd.DataFrame,
) -> pd.DataFrame:
    """Keep the top half of scored active products and de-emphasize the lower half.

    Products without completed turnover evidence fail open to their raw core target.
    Removed risk is never reallocated.
    """
    raw = raw_weights.astype(float).copy()
    ranked = score.reindex(index=raw.index, columns=raw.columns)
    result = raw.copy()

    for position in range(len(raw)):
        row = raw.iloc[position]
        active = row.abs() > 1e-15
        evidence = ranked.iloc[position].where(active).dropna()
        if len(evidence) <= 1:
            continue
        keep_count = max(1, ceil(len(evidence) / 2.0))
        ordered = evidence.sort_values(ascending=False, kind="mergesort")
        keep = set(ordered.index[:keep_count])
        lower = [product for product in evidence.index if product not in keep]
        if lower:
            result.loc[raw.index[position], lower] = row.loc[lower] * ALPHA_EFFICIENCY_CORE_SHARE

    if bool((result.abs() > raw.abs() + 1e-12).any().any()):
        raise AssertionError("alpha-efficiency selector increased product risk")
    if bool(((result * raw) < -1e-12).any().any()):
        raise AssertionError("alpha-efficiency selector flipped product direction")
    if bool((result.abs().sum(axis=1) > raw.abs().sum(axis=1) + 1e-12).any()):
        raise AssertionError("alpha-efficiency selector increased portfolio gross")
    return result
