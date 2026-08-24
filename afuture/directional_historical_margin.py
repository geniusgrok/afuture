"""Research-only point-in-time historical margin adapter.

The validated live runtime is not wired to this module. A decision day may use only a
strictly earlier exact-symbol exchange margin observation; missing evidence falls back to
the caller's existing scenario proxy. The existing 1.25 estimate buffer and all account
hard gates remain authoritative.
"""
from __future__ import annotations

from math import isfinite

import pandas as pd

from .directional_robustness import MarginAwareDirectionalProductionAcceptance


class HistoricalMarginAwareDirectionalProductionAcceptance(
    MarginAwareDirectionalProductionAcceptance
):
    """Use causal exact-symbol historical margins when available."""

    def __init__(self, config=None, *, margin_history: pd.DataFrame) -> None:
        super().__init__(config)
        frame = margin_history.copy()
        required = {"date", "symbol", "conservative_margin_ratio"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"historical margin missing columns: {sorted(missing)}")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        frame["symbol"] = frame["symbol"].astype(str).str.upper()
        frame["conservative_margin_ratio"] = pd.to_numeric(
            frame["conservative_margin_ratio"], errors="coerce"
        )
        frame = frame.dropna(
            subset=["date", "symbol", "conservative_margin_ratio"]
        )
        frame = frame[
            (frame["conservative_margin_ratio"] > 0.0)
            & (frame["conservative_margin_ratio"] < 1.0)
        ].copy()
        if not frame.empty:
            frame = (
                frame.groupby(["date", "symbol"], as_index=False)[
                    "conservative_margin_ratio"
                ]
                .max()
                .sort_values(["symbol", "date"])
            )
        self._historical_margin = frame
        self._margin_by_symbol = {
            str(symbol): group.set_index("date")["conservative_margin_ratio"].sort_index()
            for symbol, group in frame.groupby("symbol", sort=False)
        }
        self._active_margin_day: pd.Timestamp | None = None

    def _on_simulation_day(self, day: pd.Timestamp) -> None:
        self._active_margin_day = pd.Timestamp(day).normalize()

    def margin_rate(self, symbol: str) -> float:
        fallback = super().margin_rate(symbol)
        if self._active_margin_day is None:
            return fallback
        series = self._margin_by_symbol.get(str(symbol).upper())
        if series is None or series.empty:
            return fallback
        prior = series.loc[series.index < self._active_margin_day]
        if prior.empty:
            return fallback
        value = float(prior.iloc[-1])
        if not isfinite(value) or not 0.0 < value < 1.0:
            return fallback
        return value
