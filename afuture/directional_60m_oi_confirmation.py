"""Batch adapters for the shared causal 60-minute Price x OI primitives.

The candidate has no fitted threshold or lookback. For each completed product session it
selects the contract with the highest final 60-minute open interest (then total volume,
then symbol), takes the direction of first-open -> last-close only when that contract's
open interest increased during the session, and exposes that direction to the next frozen
target session. The shared Stress-90 primitive confirms entries, same-sign increases and
reversals; unconfirmed reversals exit to flat. Reductions and exits remain authoritative,
and unsupported products are unchanged. Missing evidence remains missing rather than being
silently converted to a legal zero flow.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from .directional_stress90_policy import (
    SUPPORTED_OI_PRODUCTS,
    apply_oi_confirmation_row,
)

# Frozen from coverage workflow 32717335780. Each product has >=80% exact daily 60m
# coverage in both 2022-08-21..2024-08-20 and 2024-08-21..2026-08-20, and the set spans
# at least two exchanges. Do not expand this set from later provider responses inside
# this research phase.
SUPPORTED_PRODUCTS = SUPPORTED_OI_PRODUCTS

_REQUIRED_COLUMNS = {
    "datetime",
    "product",
    "symbol",
    "open",
    "close",
    "volume",
    "hold",
}


def build_daily_price_oi_flow(
    raw: pd.DataFrame,
    *,
    trading_day_column: str | None = None,
) -> pd.DataFrame:
    """Build parameter-free completed-session direction from 60-minute contract bars.

    ``trading_day_column`` lets offline data adapters carry an exchange trading-day
    mapping for bars whose timestamp falls on the prior calendar evening. Omitting it
    retains the frozen historical calendar-date grouping.
    """
    missing = _REQUIRED_COLUMNS - set(raw.columns)
    if missing:
        raise ValueError(f"60m OI confirmation missing columns: {sorted(missing)}")
    if trading_day_column is not None and trading_day_column not in raw.columns:
        raise ValueError(f"60m OI confirmation missing trading-day column: {trading_day_column}")

    frame = raw.copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    for column in ("open", "close", "volume", "hold"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(
        subset=["datetime", "product", "symbol", "open", "close", "volume", "hold"]
    )
    frame = frame[
        (frame["open"] > 0.0)
        & (frame["close"] > 0.0)
        & (frame["volume"] >= 0.0)
        & (frame["hold"] >= 0.0)
    ].copy()
    if frame.empty:
        return pd.DataFrame(dtype=float)

    if trading_day_column is None:
        frame["date"] = frame["datetime"].dt.normalize()
    else:
        frame["date"] = pd.to_datetime(frame[trading_day_column], errors="coerce").dt.normalize()
        frame = frame.dropna(subset=["date"])
    frame.sort_values(["date", "product", "symbol", "datetime"], inplace=True)
    contract = frame.groupby(["date", "product", "symbol"], as_index=False, sort=False).agg(
        first_open=("open", "first"),
        last_close=("close", "last"),
        first_hold=("hold", "first"),
        last_hold=("hold", "last"),
        total_volume=("volume", "sum"),
    )
    contract.sort_values(
        ["date", "product", "last_hold", "total_volume", "symbol"],
        ascending=[True, True, False, False, True],
        inplace=True,
    )
    dominant = contract.groupby(["date", "product"], as_index=False, sort=False).first()
    intraday = dominant["last_close"].div(dominant["first_open"]) - 1.0
    intraday_values = intraday.to_numpy(float)
    direction = np.sign(intraday_values)
    direction[~np.isfinite(intraday_values)] = 0.0
    direction[np.abs(intraday_values) <= 1e-15] = 0.0
    direction[dominant["last_hold"].to_numpy(float) <= dominant["first_hold"].to_numpy(float)] = 0.0
    dominant["flow"] = direction
    result = dominant.pivot(index="date", columns="product", values="flow").sort_index()
    result.index = pd.DatetimeIndex(result.index).normalize()
    result.columns = [str(column).upper() for column in result.columns]
    return result.astype(float)


def lag_flow_to_target_days(
    flow: pd.DataFrame,
    *,
    target_days: Iterable,
    products: Iterable[str],
) -> pd.DataFrame:
    """Expose D completed 60m flow only to the immediately next target session D+1."""
    index = pd.DatetimeIndex(pd.to_datetime(list(target_days), errors="coerce")).normalize()
    index = index[~index.isna()]
    columns = [str(product).upper() for product in products]
    frame = flow.copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, errors="coerce")).normalize()
    frame = frame[~frame.index.isna()]
    frame.columns = [str(column).upper() for column in frame.columns]
    frame = frame.reindex(columns=columns).sort_index()
    result = pd.DataFrame(float("nan"), index=index, columns=columns, dtype=float)
    for position, target_day in enumerate(index):
        if position:
            source_day = index[position - 1]
        else:
            prior = frame.index[frame.index < target_day]
            if prior.empty:
                continue
            source_day = prior[-1]
        if source_day in frame.index:
            result.loc[target_day] = frame.loc[source_day]
    return result.astype(float)


def apply_oi_confirmation_to_weights(
    *,
    raw_weights: pd.DataFrame,
    confirming_flow: pd.DataFrame,
    supported_products: Iterable[str],
) -> pd.DataFrame:
    """Suppress only unsupported new/same-sign risk increases; never create exposure."""
    raw = raw_weights.copy().astype(float)
    raw.index = pd.DatetimeIndex(pd.to_datetime(raw.index, errors="coerce")).normalize()
    raw.columns = [str(column).upper() for column in raw.columns]
    flow = confirming_flow.copy().astype(float)
    flow.index = pd.DatetimeIndex(pd.to_datetime(flow.index, errors="coerce")).normalize()
    flow.columns = [str(column).upper() for column in flow.columns]
    if not np.isfinite(raw.to_numpy()).all():
        raise ValueError("raw OI weights must be finite")
    flow = flow.reindex(index=raw.index, columns=raw.columns)
    supported = tuple(str(product).upper() for product in supported_products)

    result = pd.DataFrame(0.0, index=raw.index, columns=raw.columns)
    previous = {str(product): 0.0 for product in raw.columns}
    for day in raw.index:
        completed = {
            product: (
                None
                if product not in flow.columns or pd.isna(flow.at[day, product])
                else float(flow.at[day, product])
            )
            for product in supported
            if product in raw.columns
        }
        applied = apply_oi_confirmation_row(
            raw_weights=raw.loc[day].to_dict(),
            prior_applied=previous,
            completed_flow=completed,
            supported_products=supported,
        )
        result.loc[day] = pd.Series(applied).reindex(raw.columns)
        previous = applied

    raw_gross = raw.abs().sum(axis=1)
    result_gross = result.abs().sum(axis=1)
    if bool((result_gross > raw_gross + 1e-10).any()):
        raise AssertionError("60m OI confirmation increased raw gross exposure")
    return result.astype(float)
