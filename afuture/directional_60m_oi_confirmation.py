"""Research-only causal 60-minute Price x OI confirmation overlay.

The candidate has no fitted threshold or lookback. For each completed product session it
selects the contract with the highest final 60-minute open interest (then total volume,
then symbol), takes the direction of first-open -> last-close only when that contract's
open interest increased during the session, and exposes that direction to the next frozen
target session. The overlay may suppress only new risk or same-sign increases. Reductions,
exits and reversals remain authoritative and unsupported products are unchanged.
"""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

_REQUIRED_COLUMNS = {
    "datetime",
    "product",
    "symbol",
    "open",
    "close",
    "volume",
    "hold",
}


def build_daily_price_oi_flow(raw: pd.DataFrame) -> pd.DataFrame:
    """Build parameter-free completed-session direction from 60-minute contract bars."""
    missing = _REQUIRED_COLUMNS - set(raw.columns)
    if missing:
        raise ValueError(f"60m OI confirmation missing columns: {sorted(missing)}")

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

    frame["date"] = frame["datetime"].dt.normalize()
    frame.sort_values(["date", "product", "symbol", "datetime"], inplace=True)
    contract = (
        frame.groupby(["date", "product", "symbol"], as_index=False, sort=False)
        .agg(
            first_open=("open", "first"),
            last_close=("close", "last"),
            first_hold=("hold", "first"),
            last_hold=("hold", "last"),
            total_volume=("volume", "sum"),
        )
    )
    contract.sort_values(
        ["date", "product", "last_hold", "total_volume", "symbol"],
        ascending=[True, True, False, False, True],
        inplace=True,
    )
    dominant = contract.groupby(["date", "product"], as_index=False, sort=False).first()
    intraday = dominant["last_close"].div(dominant["first_open"]) - 1.0
    direction = np.sign(intraday.to_numpy(float))
    direction[~np.isfinite(intraday.to_numpy(float))] = 0.0
    direction[np.abs(intraday.to_numpy(float)) <= 1e-15] = 0.0
    direction[
        dominant["last_hold"].to_numpy(float)
        <= dominant["first_hold"].to_numpy(float)
    ] = 0.0
    dominant["flow"] = direction
    result = dominant.pivot(index="date", columns="product", values="flow").sort_index()
    result.index = pd.DatetimeIndex(result.index).normalize()
    result.columns = [str(column).upper() for column in result.columns]
    return result.fillna(0.0).astype(float)


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
    frame = frame.reindex(index=index, columns=columns).fillna(0.0)
    return frame.shift(1).fillna(0.0).astype(float)


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
    flow = flow.reindex(index=raw.index, columns=raw.columns).fillna(0.0)
    supported = {str(product).upper() for product in supported_products}

    result = pd.DataFrame(0.0, index=raw.index, columns=raw.columns)
    previous = {str(product): 0.0 for product in raw.columns}
    for day in raw.index:
        for product in raw.columns:
            target = float(raw.at[day, product])
            prior = float(previous[product])
            final = target
            if product in supported:
                same_direction_increase = (
                    abs(target) > abs(prior) + 1e-15
                    and (abs(prior) <= 1e-15 or np.sign(target) == np.sign(prior))
                )
                if same_direction_increase:
                    evidence = float(flow.at[day, product])
                    if evidence != float(np.sign(target)):
                        final = prior
            result.at[day, product] = final
            previous[product] = final

    raw_gross = raw.abs().sum(axis=1)
    result_gross = result.abs().sum(axis=1)
    if bool((result_gross > raw_gross + 1e-10).any()):
        raise AssertionError("60m OI confirmation increased raw gross exposure")
    return result.astype(float)
