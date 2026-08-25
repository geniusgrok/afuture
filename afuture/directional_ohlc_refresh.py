"""Explicit OHLC refresh and cache-only Stress-90 loading boundary."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from .directional_ohlc_cache import (
    DirectionalOHLCCacheEntry,
    DirectionalOHLCCacheStore,
    canonicalize_ohlc_frames,
    require_unchanged_overlap,
)

_MIN_STRESS90_HISTORY = 140


def _day(raw: str, *, name: str) -> pd.Timestamp:
    if not isinstance(raw, str):
        raise RuntimeError(f"{name} must be YYYYMMDD")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d")
    except ValueError as exc:
        raise RuntimeError(f"{name} must be YYYYMMDD") from exc
    if parsed.strftime("%Y%m%d") != raw:
        raise RuntimeError(f"{name} must be YYYYMMDD")
    return pd.Timestamp(parsed.date())


def load_stress90_completed_ohlc(
    store: DirectionalOHLCCacheStore,
    *,
    products: tuple[str, ...],
    current_ctp_trading_day: str,
    required_completed_day: str | None = None,
) -> DirectionalOHLCCacheEntry:
    """Load verified bytes only; no provider/network object is accepted here."""

    current = _day(current_ctp_trading_day, name="current CTP trading day")
    entry = store.load(products)
    if entry is None:
        raise RuntimeError("verified Stress-90 OHLC cache is missing")
    if entry.row_count < _MIN_STRESS90_HISTORY:
        raise RuntimeError("verified Stress-90 OHLC cache is shorter than 140 days")
    if bool((entry.close.index >= current).any()):
        raise RuntimeError("verified Stress-90 OHLC cache contains current/future data")
    if required_completed_day is not None:
        required = _day(required_completed_day, name="required completed OHLC day")
        if required not in entry.close.index:
            raise RuntimeError(
                "verified Stress-90 OHLC cache does not cover required completed day"
            )
    return entry


def refresh_directional_ohlc_cache(
    store: DirectionalOHLCCacheStore,
    *,
    provider,
    products: tuple[str, ...],
    current_ctp_trading_day: str,
) -> DirectionalOHLCCacheEntry:
    """Fetch outside the engine, reject revisions, and atomically append verified data."""

    current = _day(current_ctp_trading_day, name="current CTP trading day")
    history = provider.load(products)
    if not hasattr(history, "open") or not hasattr(history, "close"):
        raise RuntimeError("directional OHLC provider returned an invalid history")
    open_prices, close = canonicalize_ohlc_frames(
        products,
        history.open,
        history.close,
        name="directional OHLC refresh",
    )
    if bool((close.index >= current).any()):
        raise RuntimeError("directional OHLC provider returned current/future data")
    existing = store.load(products)
    if existing is not None:
        require_unchanged_overlap(existing, open_prices, close)
    return store.save(products, open_prices, close)
