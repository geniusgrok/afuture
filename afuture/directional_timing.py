"""Causal minute/L1 execution timing primitives for directional openings.

This module never owns Alpha, target size, account state, or risk authority. It may only
suggest delaying a normal opening/same-sign increase for at most the fixed opening
window. Reductions, exits and reversals are explicitly ineligible for delay.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping
from zoneinfo import ZoneInfo

from .models import ContractSpec, OrderSide, Tick


_CHINA_TZ = ZoneInfo("Asia/Shanghai")


class TimingDecision(str, Enum):
    WAIT = "wait"
    EXECUTE = "execute"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class TimingFeatures:
    session: str
    elapsed_minutes: int
    last_price: float
    opening_gap: float | None
    opening_5m_high: float
    opening_5m_low: float
    opening_15m_high: float
    opening_15m_low: float
    vwap: float
    relative_volume: float | None
    open_interest_change: float
    spread_ticks: float
    book_imbalance: float
    opposite_depth_buy: int
    opposite_depth_sell: int


def opening_delta_can_be_delayed(current_lots: int, target_lots: int) -> bool:
    """Return True only for new exposure or a same-direction absolute increase."""
    current = int(current_lots)
    target = int(target_lots)
    if target == 0:
        return False
    if current == 0:
        return True
    if (current > 0) != (target > 0):
        return False
    return abs(target) > abs(current)


def _session(timestamp: datetime) -> str:
    local = timestamp.astimezone(_CHINA_TZ)
    return "night" if local.hour >= 20 or local.hour < 3 else "day"


class MinuteTimingOverlay:
    """Track one contract session and return a bounded execution-timing decision."""

    def __init__(
        self,
        *,
        previous_close: float | None = None,
        completed_same_clock_volume: Mapping[int, float] | None = None,
        stale_after_seconds: float = 90.0,
    ) -> None:
        self.previous_close = (
            float(previous_close)
            if previous_close is not None and float(previous_close) > 0
            else None
        )
        self.completed_same_clock_volume = {
            int(key): float(value)
            for key, value in (completed_same_clock_volume or {}).items()
            if int(key) >= 0 and float(value) > 0
        }
        self.stale_after_seconds = max(1.0, float(stale_after_seconds))
        self._reset()

    def _reset(self) -> None:
        self._session_key: tuple[str, str] | None = None
        self._start_timestamp: datetime | None = None
        self._last_timestamp: datetime | None = None
        self._first_price = 0.0
        self._last_price = 0.0
        self._base_volume = 0.0
        self._last_volume = 0.0
        self._cum_volume = 0.0
        self._vwap_notional = 0.0
        self._first_oi = 0.0
        self._last_oi = 0.0
        self._opening_5m_high = float("-inf")
        self._opening_5m_low = float("inf")
        self._opening_15m_high = float("-inf")
        self._opening_15m_low = float("inf")
        self._features: TimingFeatures | None = None

    def _baseline_volume(self, elapsed_minutes: int) -> float | None:
        eligible = [
            minute
            for minute in self.completed_same_clock_volume
            if minute <= elapsed_minutes
        ]
        if not eligible:
            return None
        return self.completed_same_clock_volume[max(eligible)]

    def observe(self, tick: Tick, spec: ContractSpec) -> TimingFeatures:
        tick.validate()
        price_tick = float(spec.price_tick)
        if price_tick <= 0:
            raise ValueError("price_tick must be positive")
        session = _session(tick.timestamp)
        key = (str(tick.trading_day), session)
        if self._session_key != key:
            self._reset()
            self._session_key = key
            self._start_timestamp = tick.timestamp
            self._first_price = float(tick.last_price)
            self._base_volume = float(tick.volume)
            self._last_volume = float(tick.volume)
            self._first_oi = float(tick.open_interest)

        assert self._start_timestamp is not None
        elapsed = max(
            0,
            int((tick.timestamp - self._start_timestamp).total_seconds() // 60),
        )
        last = float(tick.last_price)
        current_volume = float(tick.volume)
        if current_volume >= self._last_volume:
            delta_volume = current_volume - self._last_volume
        else:
            # A feed/session counter reset must not turn into negative volume or future
            # knowledge. Restart the cumulative baseline from the observation itself.
            delta_volume = 0.0
            self._base_volume = current_volume
            self._cum_volume = 0.0
            self._vwap_notional = 0.0
        if delta_volume > 0:
            self._cum_volume += delta_volume
            self._vwap_notional += last * delta_volume
        self._last_volume = current_volume
        self._last_price = last
        self._last_oi = float(tick.open_interest)
        self._last_timestamp = tick.timestamp

        if elapsed <= 5:
            self._opening_5m_high = max(self._opening_5m_high, last)
            self._opening_5m_low = min(self._opening_5m_low, last)
        if elapsed <= 15:
            self._opening_15m_high = max(self._opening_15m_high, last)
            self._opening_15m_low = min(self._opening_15m_low, last)

        vwap = (
            self._vwap_notional / self._cum_volume
            if self._cum_volume > 0
            else last
        )
        baseline_volume = self._baseline_volume(elapsed)
        relative_volume = (
            self._cum_volume / baseline_volume
            if baseline_volume is not None and baseline_volume > 0
            else None
        )
        opening_gap = (
            self._first_price / self.previous_close - 1.0
            if self.previous_close is not None
            else None
        )
        depth_sum = float(tick.bid_volume) + float(tick.ask_volume)
        imbalance = (
            (float(tick.bid_volume) - float(tick.ask_volume)) / depth_sum
            if depth_sum > 0
            else 0.0
        )
        opening_5m_high = (
            self._opening_5m_high
            if self._opening_5m_high != float("-inf")
            else last
        )
        opening_5m_low = (
            self._opening_5m_low
            if self._opening_5m_low != float("inf")
            else last
        )
        opening_15m_high = (
            self._opening_15m_high
            if self._opening_15m_high != float("-inf")
            else last
        )
        opening_15m_low = (
            self._opening_15m_low
            if self._opening_15m_low != float("inf")
            else last
        )
        self._features = TimingFeatures(
            session=session,
            elapsed_minutes=elapsed,
            last_price=last,
            opening_gap=opening_gap,
            opening_5m_high=opening_5m_high,
            opening_5m_low=opening_5m_low,
            opening_15m_high=opening_15m_high,
            opening_15m_low=opening_15m_low,
            vwap=float(vwap),
            relative_volume=(
                float(relative_volume) if relative_volume is not None else None
            ),
            open_interest_change=float(self._last_oi - self._first_oi),
            spread_ticks=float((float(tick.ask_price) - float(tick.bid_price)) / price_tick),
            book_imbalance=float(imbalance),
            opposite_depth_buy=max(0, int(tick.ask_volume)),
            opposite_depth_sell=max(0, int(tick.bid_volume)),
        )
        return self._features

    @property
    def features(self) -> TimingFeatures | None:
        return self._features

    def decision(
        self,
        side: OrderSide,
        *,
        requested_volume: int,
        now: datetime | None = None,
    ) -> TimingDecision:
        volume = int(requested_volume)
        if volume <= 0:
            raise ValueError("requested_volume must be positive")
        features = self._features
        if features is None or self._last_timestamp is None:
            return TimingDecision.FALLBACK
        observed_now = now or self._last_timestamp
        if (observed_now - self._last_timestamp).total_seconds() > self.stale_after_seconds:
            return TimingDecision.FALLBACK
        if features.elapsed_minutes < 5:
            return TimingDecision.WAIT
        if features.elapsed_minutes >= 15:
            # Timing is never allowed to create an unbounded execution delay.
            return TimingDecision.EXECUTE

        liquid = features.spread_ticks <= 1.0 + 1e-12
        relative_volume_ok = (
            features.relative_volume is None or features.relative_volume >= 1.0
        )
        if side is OrderSide.BUY:
            depth_ok = features.opposite_depth_buy >= volume
            non_chasing = (
                features.last_price <= features.vwap + 1e-12
                and features.last_price <= features.opening_5m_high + 1e-12
            )
            book_ok = features.book_imbalance >= 0.0
        elif side is OrderSide.SELL:
            depth_ok = features.opposite_depth_sell >= volume
            non_chasing = (
                features.last_price >= features.vwap - 1e-12
                and features.last_price >= features.opening_5m_low - 1e-12
            )
            book_ok = features.book_imbalance <= 0.0
        else:
            return TimingDecision.FALLBACK
        return (
            TimingDecision.EXECUTE
            if liquid and relative_volume_ok and depth_ok and non_chasing and book_ok
            else TimingDecision.WAIT
        )
