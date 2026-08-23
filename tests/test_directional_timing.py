from datetime import datetime, timedelta, timezone
import importlib

from afuture.models import ContractSpec, OrderSide, Tick


BASE = datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc)  # 21:00 China, night session
SPEC = ContractSpec("RBX", "SHFE", 10, 1, 0.15, 0.15)


def _tick(
    minute: int,
    *,
    last: float,
    bid: float,
    ask: float,
    bid_volume: float,
    ask_volume: float,
    volume: float,
    oi: float,
) -> Tick:
    return Tick(
        symbol="RBX",
        exchange="SHFE",
        timestamp=BASE + timedelta(minutes=minute),
        bid_price=bid,
        ask_price=ask,
        last_price=last,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        trading_day="20260825",
        volume=volume,
        open_interest=oi,
    )


def _module():
    return importlib.import_module("afuture.directional_timing")


def test_only_opening_or_same_sign_increase_can_be_delayed():
    timing = _module()
    assert timing.opening_delta_can_be_delayed(0, 3) is True
    assert timing.opening_delta_can_be_delayed(2, 3) is True
    assert timing.opening_delta_can_be_delayed(-2, -3) is True
    assert timing.opening_delta_can_be_delayed(3, 2) is False
    assert timing.opening_delta_can_be_delayed(-3, -2) is False
    assert timing.opening_delta_can_be_delayed(2, -2) is False
    assert timing.opening_delta_can_be_delayed(2, 0) is False


def test_minute_overlay_builds_causal_opening_vwap_oi_volume_and_l1_features():
    timing = _module()
    overlay = timing.MinuteTimingOverlay(
        previous_close=100.0,
        completed_same_clock_volume={5: 80.0},
    )
    overlay.observe(
        _tick(
            0,
            last=101,
            bid=100,
            ask=101,
            bid_volume=12,
            ask_volume=8,
            volume=100,
            oi=1000,
        ),
        SPEC,
    )
    features = overlay.observe(
        _tick(
            5,
            last=100,
            bid=99.5,
            ask=100.5,
            bid_volume=15,
            ask_volume=5,
            volume=180,
            oi=1020,
        ),
        SPEC,
    )
    assert features.session == "night"
    assert features.elapsed_minutes == 5
    assert round(features.opening_gap, 8) == 0.01
    assert features.opening_5m_high == 101
    assert features.opening_5m_low == 100
    assert features.vwap == 100
    assert features.relative_volume == 1.0
    assert features.open_interest_change == 20
    assert features.spread_ticks == 1.0
    assert features.book_imbalance == 0.5
    assert features.opposite_depth_buy == 5
    assert features.opposite_depth_sell == 15


def test_minute_overlay_waits_before_five_minutes_then_executes_non_chasing_liquid_buy():
    timing = _module()
    overlay = timing.MinuteTimingOverlay(previous_close=100.0, completed_same_clock_volume={5: 80})
    overlay.observe(
        _tick(0, last=101, bid=100, ask=101, bid_volume=12, ask_volume=8, volume=100, oi=1000),
        SPEC,
    )
    assert overlay.decision(OrderSide.BUY, requested_volume=5) is timing.TimingDecision.WAIT
    overlay.observe(
        _tick(5, last=100, bid=99.5, ask=100.5, bid_volume=15, ask_volume=5, volume=180, oi=1020),
        SPEC,
    )
    assert overlay.decision(OrderSide.BUY, requested_volume=5) is timing.TimingDecision.EXECUTE


def test_minute_overlay_does_not_chase_before_fifteen_minutes_but_has_bounded_delay():
    timing = _module()
    overlay = timing.MinuteTimingOverlay(previous_close=100.0)
    overlay.observe(
        _tick(0, last=100, bid=99.5, ask=100.5, bid_volume=10, ask_volume=10, volume=100, oi=1000),
        SPEC,
    )
    overlay.observe(
        _tick(6, last=104, bid=103, ask=105, bid_volume=2, ask_volume=2, volume=200, oi=1020),
        SPEC,
    )
    assert overlay.decision(OrderSide.BUY, requested_volume=5) is timing.TimingDecision.WAIT
    overlay.observe(
        _tick(15, last=105, bid=103, ask=107, bid_volume=1, ask_volume=1, volume=300, oi=1040),
        SPEC,
    )
    assert overlay.decision(OrderSide.BUY, requested_volume=5) is timing.TimingDecision.EXECUTE


def test_missing_or_stale_minute_evidence_falls_back_instead_of_blocking_execution():
    timing = _module()
    overlay = timing.MinuteTimingOverlay(previous_close=100.0, stale_after_seconds=90)
    assert overlay.decision(OrderSide.SELL, requested_volume=2) is timing.TimingDecision.FALLBACK
    tick = _tick(0, last=100, bid=99.5, ask=100.5, bid_volume=10, ask_volume=10, volume=100, oi=1000)
    overlay.observe(tick, SPEC)
    assert overlay.decision(
        OrderSide.SELL,
        requested_volume=2,
        now=tick.timestamp + timedelta(seconds=91),
    ) is timing.TimingDecision.FALLBACK
