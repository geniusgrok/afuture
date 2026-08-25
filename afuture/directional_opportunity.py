"""Causal product-opportunity ranking for the directional portfolio.

This module is deliberately directionless: it answers whether an already-active raw
product target is a comparatively attractive opportunity, not whether the product should
be long or short. It may only preserve or reduce risk created by the frozen directional
core; it never creates, flips, or enlarges exposure.
"""

from __future__ import annotations

from math import ceil, sqrt

import numpy as np
import pandas as pd

OPPORTUNITY_CORE_SHARE = 0.75
SHORT_WINDOW = 20
LONG_WINDOW = 60
MAX_ABS_DAILY_RETURN = 0.20
MAX_STRENGTH = 5.0
MAX_PARTICIPATION = 5.0


def _clean_close(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result.index = pd.to_datetime(result.index, errors="coerce")
    result = result[~result.index.isna()].sort_index()
    result.columns = [str(item).upper() for item in result.columns]
    result = result.astype(float)
    return result.where(result > 0.0)


def _align_activity(
    frame: pd.DataFrame | None,
    close: pd.DataFrame,
) -> pd.DataFrame | None:
    if frame is None:
        return None
    result = frame.copy()
    result.index = pd.to_datetime(result.index, errors="coerce")
    result = result[~result.index.isna()].sort_index()
    result.columns = [str(item).upper() for item in result.columns]
    result = result.reindex(index=close.index, columns=close.columns).astype(float)
    return result.where(result > 0.0)


def _rolling_log_return(returns: pd.DataFrame, window: int) -> pd.DataFrame:
    return np.log1p(returns.clip(lower=-0.99)).rolling(window, min_periods=window).sum()


def _cross_sectional_rank(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rank(axis=1, method="average", pct=True)


def build_opportunity_score(
    close: pd.DataFrame,
    volume: pd.DataFrame | None,
    open_interest: pd.DataFrame | None,
) -> pd.DataFrame:
    """Return a one-session-lagged cross-sectional product opportunity score.

    Every feature is calculated from completed daily observations. The final shift is
    the causality boundary: the row labelled ``t`` contains only information available
    through row ``t-1``. Activity evidence is mandatory because silently synthesizing
    volume/open-interest participation would change the meaning of the score.
    """
    prices = _clean_close(close)
    traded_volume = _align_activity(volume, prices)
    open_positions = _align_activity(open_interest, prices)
    if traded_volume is None or open_positions is None:
        return pd.DataFrame(np.nan, index=prices.index, columns=prices.columns)

    returns = prices.pct_change(fill_method=None)
    returns = returns.mask(returns.abs() > MAX_ABS_DAILY_RETURN)
    momentum20 = _rolling_log_return(returns, SHORT_WINDOW)
    momentum60 = _rolling_log_return(returns, LONG_WINDOW)

    path20 = (
        returns.abs().rolling(SHORT_WINDOW, min_periods=SHORT_WINDOW).sum().replace(0.0, np.nan)
    )
    path60 = returns.abs().rolling(LONG_WINDOW, min_periods=LONG_WINDOW).sum().replace(0.0, np.nan)
    efficiency20 = momentum20.abs().div(path20)
    efficiency60 = momentum60.abs().div(path60)

    volatility20 = (
        returns.rolling(SHORT_WINDOW, min_periods=SHORT_WINDOW).std().replace(0.0, np.nan)
    )
    strength20 = (
        momentum20.abs()
        .div(volatility20 * sqrt(float(SHORT_WINDOW)))
        .clip(lower=0.0, upper=MAX_STRENGTH)
    )

    rolling_low = prices.rolling(SHORT_WINDOW, min_periods=SHORT_WINDOW).min()
    rolling_high = prices.rolling(SHORT_WINDOW, min_periods=SHORT_WINDOW).max()
    range_width = (rolling_high - rolling_low).replace(0.0, np.nan)
    breakout_location = ((prices - rolling_low).div(range_width).sub(0.5).abs().mul(2.0)).clip(
        lower=0.0, upper=1.0
    )

    volume_median = (
        traded_volume.rolling(SHORT_WINDOW, min_periods=SHORT_WINDOW).median().replace(0.0, np.nan)
    )
    volume_participation = traded_volume.div(volume_median).clip(lower=0.0, upper=MAX_PARTICIPATION)
    oi_median = (
        open_positions.rolling(SHORT_WINDOW, min_periods=SHORT_WINDOW).median().replace(0.0, np.nan)
    )
    oi_participation = open_positions.div(oi_median).clip(lower=0.0, upper=MAX_PARTICIPATION)

    ranks = [
        _cross_sectional_rank(component)
        for component in (
            efficiency20,
            efficiency60,
            strength20,
            breakout_location,
            volume_participation,
            oi_participation,
        )
    ]
    score = ranks[0]
    for component in ranks[1:]:
        score = score + component
    score = score / float(len(ranks))
    return score.shift(1)


def apply_opportunity_overlay(
    raw_weights: pd.DataFrame,
    score: pd.DataFrame,
) -> pd.DataFrame:
    """De-emphasize the lower-ranked half of active raw product targets.

    High-opportunity active products keep 100% of their raw weight. Every other active
    product is held at ``OPPORTUNITY_CORE_SHARE`` of raw weight. If no active product has
    usable evidence, the row fails open to the frozen core exactly.
    """
    raw = raw_weights.astype(float).copy()
    ranked = score.reindex(index=raw.index, columns=raw.columns)
    result = raw.copy()

    for position in range(len(raw)):
        row = raw.iloc[position]
        active = row.abs() > 1e-15
        if not bool(active.any()):
            continue
        evidence = ranked.iloc[position].where(active).dropna()
        if evidence.empty:
            continue
        keep_count = max(1, ceil(len(evidence) / 2.0))
        ordered = evidence.sort_values(ascending=False, kind="mergesort")
        keep = set(ordered.index[:keep_count])

        adjusted = row.copy()
        adjusted.loc[active] = adjusted.loc[active] * OPPORTUNITY_CORE_SHARE
        if keep:
            adjusted.loc[list(keep)] = row.loc[list(keep)]
        result.iloc[position] = adjusted

    if bool((result.abs() > raw.abs() + 1e-12).any().any()):
        raise AssertionError("opportunity overlay increased product risk")
    if bool(((result * raw) < -1e-12).any().any()):
        raise AssertionError("opportunity overlay flipped product direction")
    raw_gross = raw.abs().sum(axis=1)
    result_gross = result.abs().sum(axis=1)
    if bool((result_gross > raw_gross + 1e-12).any()):
        raise AssertionError("opportunity overlay increased portfolio gross")
    return result
