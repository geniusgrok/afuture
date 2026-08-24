"""Research-only cost-aware no-trade filter from the predeclared PR #17 candidate.

The filter is deliberately narrow: it may suppress only new risk and same-direction
absolute increases. Reductions, exits and reversals are never delayed. A target-session
decision uses only a completed 20-session product return through the previous close and
permits an increase only when the direction-conditioned three-session benefit estimate
strictly exceeds the fixed 15bp one-way cost hurdle.
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

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
    # At target session D the most recent admissible close is D-1. Therefore the
    # completed 20-session return available at D is close[D-1] / close[D-21] - 1.
    return close.pct_change(
        periods=TREND_LOOKBACK_SESSIONS,
        fill_method=None,
    ).shift(1)


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
    hurdle = COST_HURDLE_BPS / 10000.0
    horizon_scale = BENEFIT_HORIZON_SESSIONS / TREND_LOOKBACK_SESSIONS

    for timestamp in raw.index:
        next_state: dict[str, float] = {}
        for product in raw.columns:
            target = float(raw.at[timestamp, product])
            current = float(state.get(product, 0.0))
            same_side = (
                abs(current) > _EPS
                and abs(target) > _EPS
                and np.sign(current) == np.sign(target)
            )
            increase = (
                (abs(current) <= _EPS and abs(target) > _EPS)
                or (same_side and abs(target) > abs(current) + _EPS)
            )

            applied = target
            if increase:
                trend = float(completed.at[timestamp, product])
                expected_benefit = (
                    np.sign(target) * trend * horizon_scale
                    if np.isfinite(trend)
                    else float("nan")
                )
                if not np.isfinite(expected_benefit) or expected_benefit <= hurdle + _EPS:
                    applied = current

            if abs(applied) > _EPS:
                next_state[product] = float(applied)
                output.at[timestamp, product] = float(applied)

        state = next_state

    if bool((output.abs().sum(axis=1) > MAX_GROSS_LEVERAGE + _EPS).any()):
        raise AssertionError("cost-aware no-trade filter exceeded raw 2x gross cap")
    return output
