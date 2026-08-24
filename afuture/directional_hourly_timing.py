"""Research-only 60-minute timing confirmation for directional risk increases.

This module does not create a new Production authority. It derives a completed
Price x Open-Interest state from frozen 60-minute bars and may delay only new
positions or same-sign risk increases in an already-generated directional target.
Reductions, exits and reversals always bypass the timing confirmation. Missing
hourly evidence preserves the baseline target.
"""
from __future__ import annotations

from math import isfinite

import numpy as np
import pandas as pd

MAX_GROSS_LEVERAGE = 2.0
_EPS = 1e-12


def price_oi_direction(*, price_return: float, oi_return: float) -> int:
    """Classic Price x OI quadrant direction with no fitted threshold.

    price up / OI up       -> bullish new-long pressure
    price down / OI up     -> bearish new-short pressure
    price up / OI down     -> bearish short-covering state
    price down / OI down   -> bullish long-liquidation state
    """
    p = float(price_return)
    o = float(oi_return)
    if not isfinite(p) or not isfinite(o) or abs(p) <= _EPS or abs(o) <= _EPS:
        return 0
    return 1 if (p > 0.0) == (o > 0.0) else -1


def _map_hour_to_trading_day(
    timestamp: pd.Timestamp,
    trading_days: pd.DatetimeIndex,
) -> pd.Timestamp | pd.NaT:
    stamp = pd.Timestamp(timestamp)
    day = stamp.normalize()
    # China-futures night session belongs to the next exchange trading day.
    if stamp.hour >= 20:
        position = int(trading_days.searchsorted(day, side="right"))
        return trading_days[position] if position < len(trading_days) else pd.NaT
    position = int(trading_days.searchsorted(day, side="left"))
    if position < len(trading_days) and trading_days[position] == day:
        return trading_days[position]
    return pd.NaT


def build_completed_hourly_state(
    hourly: pd.DataFrame,
    *,
    trading_days: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Build one completed Price x OI direction per product/trading day.

    At each 60-minute timestamp the highest-open-interest contract is used, with
    volume then symbol as deterministic tie breakers. Night bars (20:00+) are
    assigned to the next frozen exchange trading day. The resulting row for D is
    completed only after D's session finishes; callers must shift it to D+1 before
    using it for a target decision.
    """
    required = {"datetime", "open", "close", "volume", "hold", "symbol", "product"}
    missing = required - set(hourly.columns)
    if missing:
        raise ValueError(f"hourly timing input missing columns: {sorted(missing)}")
    days = pd.DatetimeIndex(pd.to_datetime(trading_days, errors="coerce")).dropna().normalize().unique().sort_values()
    frame = hourly.copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    for column in ("open", "close", "volume", "hold"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["datetime", "product", "symbol", "open", "close", "hold"])
    frame = frame[(frame["open"] > 0.0) & (frame["close"] > 0.0) & (frame["hold"] >= 0.0)]
    frame["trading_day"] = frame["datetime"].map(lambda value: _map_hour_to_trading_day(value, days))
    frame = frame.dropna(subset=["trading_day"])
    if frame.empty:
        return pd.DataFrame(index=days)

    dominant = (
        frame.sort_values(
            ["product", "datetime", "hold", "volume", "symbol"],
            ascending=[True, True, True, True, True],
        )
        .groupby(["product", "datetime"], sort=False, as_index=False)
        .tail(1)
        .sort_values(["trading_day", "product", "datetime"])
    )

    rows: list[dict] = []
    for (day, product), group in dominant.groupby(["trading_day", "product"], sort=True):
        group = group.sort_values("datetime")
        first = group.iloc[0]
        last = group.iloc[-1]
        first_hold = float(first["hold"])
        if first_hold <= 0.0:
            direction = 0
        else:
            direction = price_oi_direction(
                price_return=float(last["close"]) / float(first["open"]) - 1.0,
                oi_return=float(last["hold"]) / first_hold - 1.0,
            )
        rows.append({"trading_day": pd.Timestamp(day).normalize(), "product": str(product), "direction": int(direction)})

    if not rows:
        return pd.DataFrame(index=days)
    state = pd.DataFrame(rows).pivot(index="trading_day", columns="product", values="direction")
    return state.sort_index().astype(float)


def shift_completed_state_to_next_session(
    state: pd.DataFrame,
    *,
    trading_days: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Move completed D state to the immediately following frozen trading day."""
    days = pd.DatetimeIndex(pd.to_datetime(trading_days, errors="coerce")).dropna().normalize().unique().sort_values()
    output = pd.DataFrame(index=days, columns=state.columns, dtype=float)
    normalized = state.copy()
    normalized.index = pd.to_datetime(normalized.index, errors="coerce").normalize()
    for day, row in normalized.iterrows():
        position = int(days.searchsorted(pd.Timestamp(day).normalize(), side="right"))
        if position < len(days):
            output.loc[days[position], row.index] = row.values
    return output


def apply_hourly_timing_overlay(
    raw_weights: pd.DataFrame,
    hourly_state: pd.DataFrame,
) -> pd.DataFrame:
    """Delay only unconfirmed risk increases while preserving risk reductions."""
    raw = raw_weights.copy().astype(float)
    raw.index = pd.to_datetime(raw.index, errors="coerce").normalize()
    raw = raw[~raw.index.isna()].sort_index().fillna(0.0)
    raw.columns = [str(column).upper() for column in raw.columns]
    if bool((raw.abs().sum(axis=1) > MAX_GROSS_LEVERAGE + _EPS).any()):
        raise ValueError("raw directional target exceeds 2x gross")

    state = hourly_state.copy()
    state.index = pd.to_datetime(state.index, errors="coerce").normalize()
    state = state[~state.index.isna()].sort_index()
    state.columns = [str(column).upper() for column in state.columns]

    result = raw.copy()
    effective = {column: 0.0 for column in raw.columns}
    for day in raw.index:
        for product in raw.columns:
            target = float(raw.at[day, product])
            previous = float(effective[product])

            # Exit, absolute reduction, and a true sign reversal are always allowed.
            if abs(target) <= _EPS:
                chosen = 0.0
            elif abs(previous) > _EPS and ((target > 0.0) != (previous > 0.0)):
                chosen = target
            elif abs(previous) > _EPS and (target > 0.0) == (previous > 0.0) and abs(target) <= abs(previous) + _EPS:
                chosen = target
            else:
                evidence = np.nan
                if day in state.index and product in state.columns:
                    evidence = float(state.at[day, product])
                if not np.isfinite(evidence) or abs(evidence) <= _EPS:
                    chosen = target
                elif (target > 0.0) == (evidence > 0.0):
                    chosen = target
                else:
                    chosen = previous if abs(previous) > _EPS else 0.0

            result.at[day, product] = chosen
            effective[product] = chosen

        if float(result.loc[day].abs().sum()) > float(raw.loc[day].abs().sum()) + _EPS:
            raise AssertionError("hourly timing overlay increased raw daily gross")
        if float(result.loc[day].abs().sum()) > MAX_GROSS_LEVERAGE + _EPS:
            raise AssertionError("hourly timing overlay exceeded 2x gross")
    return result
