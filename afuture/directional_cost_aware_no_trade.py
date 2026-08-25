"""Batch adapter for the shared cost-aware no-trade Stress-90 primitive.

The filter is deliberately narrow: it may suppress only new risk and same-direction
absolute increases. Reductions, exits and reversals are never delayed. A target-session
decision uses only the arithmetic sum of the 20 completed daily product returns through
the previous close and permits an increase only when the direction-conditioned three-
session benefit estimate strictly exceeds the fixed 15bp one-way cost hurdle.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from .directional_stress90_policy import _apply_cost_gate_from_trends_row

TREND_LOOKBACK_SESSIONS = 20
BENEFIT_HORIZON_SESSIONS = 3
COST_HURDLE_BPS = 15.0
MAX_GROSS_LEVERAGE = 2.0
_EPS = 1e-12


def _clean(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result.index = pd.to_datetime(result.index, errors="coerce")
    result = result[~result.index.isna()].sort_index()
    result.columns = [str(column).upper() for column in result.columns]
    return result.astype(float)


def _completed_twenty_session_return(close_prices: pd.DataFrame) -> pd.DataFrame:
    close = _clean(close_prices).where(lambda item: item > 0.0)
    # This intentionally reproduces the archived PR #17 research definition: sum the
    # 20 individually completed daily percentage returns, then lag one target session.
    # It is not a 20-session compounded price return. The exact definition matters for
    # the archived 440.8444x / 168.5521% / 113.4880% cheap-screen lineage.
    daily = close.pct_change(fill_method=None)
    return (
        daily.rolling(
            TREND_LOOKBACK_SESSIONS,
            min_periods=TREND_LOOKBACK_SESSIONS,
        )
        .sum()
        .shift(1)
    )


def apply_cost_aware_no_trade(
    *,
    weights: pd.DataFrame,
    close_prices: pd.DataFrame,
    initial_weights: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    """Suppress only entries/increases whose completed expected benefit cannot pay 15bp."""
    raw = _clean(weights).fillna(0.0)
    if raw.empty:
        return raw
    if bool((raw.abs().sum(axis=1) > MAX_GROSS_LEVERAGE + _EPS).any()):
        raise ValueError("raw directional target exceeds 2x gross")

    close = _clean(close_prices)
    missing = sorted(set(raw.columns) - set(close.columns))
    if missing:
        raise ValueError(f"close history missing products: {missing}")
    completed = _completed_twenty_session_return(close[raw.columns]).reindex(raw.index)

    state = {
        str(product).upper(): float(value)
        for product, value in (initial_weights or {}).items()
        if np.isfinite(float(value)) and abs(float(value)) > _EPS
    }
    output = pd.DataFrame(0.0, index=raw.index, columns=raw.columns)
    for timestamp in raw.index:
        trends = {
            product: (
                float(completed.at[timestamp, product])
                if np.isfinite(float(completed.at[timestamp, product]))
                else None
            )
            for product in raw.columns
        }
        applied = _apply_cost_gate_from_trends_row(
            oi_weights=raw.loc[timestamp].to_dict(),
            prior_approved=state,
            completed_return_sums=trends,
            completed_lookback_sessions=TREND_LOOKBACK_SESSIONS,
            benefit_horizon_sessions=BENEFIT_HORIZON_SESSIONS,
            cost_hurdle_bps=COST_HURDLE_BPS,
        )
        output.loc[timestamp] = pd.Series(applied).reindex(raw.columns)
        state = {product: value for product, value in applied.items() if abs(value) > _EPS}

    if bool((output.abs().sum(axis=1) > MAX_GROSS_LEVERAGE + _EPS).any()):
        raise AssertionError("cost-aware no-trade filter exceeded raw 2x gross cap")
    return output
