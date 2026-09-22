"""Production PairExecutor clock regressions with a fill-producing SimBroker."""

from datetime import datetime, timedelta, timezone

from afuture.broker.sim import SimBroker
from afuture.execution import PairExecutor
from afuture.models import ContractSpec, PairConfig, SignalAction, SpreadSignal, Tick
from afuture.risk import RiskConfig, RiskManager

NOW = datetime(2026, 9, 22, 1, 1, tzinfo=timezone.utc)


def setup_execution(*, quote_time=NOW, wall_clock=None, elapsed_clock=None):
    specs = {symbol: ContractSpec(symbol, "DCE", 10, 1, 0.1, 0.1) for symbol in ("N", "F")}
    broker = SimBroker(500000, specs)
    broker.start()
    ticks = tuple(
        Tick(
            symbol=symbol,
            exchange="DCE",
            timestamp=quote_time,
            bid_price=bid,
            ask_price=bid + 1,
            last_price=bid + 0.5,
            bid_volume=100,
            ask_volume=100,
            trading_day="20260922",
            volume=20000,
            open_interest=80000,
        )
        for symbol, bid in (("N", 99), ("F", 89))
    )
    for tick in ticks:
        broker.publish_tick(tick)
    broker.poll_events()
    options = {"health_clock": wall_clock or (lambda: NOW)}
    if elapsed_clock is not None:
        options["elapsed_clock"] = elapsed_clock
    executor = PairExecutor(
        broker,
        RiskManager(
            RiskConfig(max_orders_per_minute=2, min_depth_multiple=1, max_quote_age_seconds=2)
        ),
        specs,
        aggressive_ticks=0,
        slippage_ticks=0,
        **options,
    )
    return broker, executor, ticks


def execute(executor, ticks, signal_time=NOW, **kwargs):
    return executor.execute_signal(
        PairConfig("p", "N", "F", "DCE", 1),
        SpreadSignal("p", SignalAction.LONG_SPREAD, -2.0, signal_time, 11, 20, 1),
        *ticks,
        open_pair_count=0,
        spread_std=1,
        **kwargs,
    )


def test_synchronized_stale_quotes_cannot_authorize_live_orders():
    old = NOW - timedelta(seconds=30)
    broker, executor, ticks = setup_execution(quote_time=old)
    try:
        result = execute(executor, ticks, signal_time=old)
        assert not result.accepted
        assert not result.order_ids
        assert broker.get_positions() == []
    finally:
        broker.stop()


def test_live_quotes_are_checked_again_after_sizing_before_first_write():
    clock_calls = 0

    def clock():
        nonlocal clock_calls
        clock_calls += 1
        return NOW if clock_calls == 1 else NOW + timedelta(seconds=10)

    broker, executor, ticks = setup_execution(wall_clock=clock)
    try:
        result = execute(executor, ticks)
        assert not result.accepted
        assert not result.order_ids
        assert broker.get_positions() == []
    finally:
        broker.stop()


def test_late_second_leg_does_not_blindly_flatten_using_stale_prices():
    clock_calls = 0

    def clock():
        nonlocal clock_calls
        clock_calls += 1
        return NOW if clock_calls <= 2 else NOW + timedelta(seconds=10)

    broker, executor, ticks = setup_execution(wall_clock=clock)
    try:
        result = execute(executor, ticks)
        assert not result.accepted
        assert len(result.order_ids) == 1
        # A sent/filled leg is real exposure, not a failed-batch fiction.  The
        # existing engine handles its imbalance; stale-price rollback cannot.
        assert sum(p.long_total + p.short_total for p in broker.get_positions()) == 1
    finally:
        broker.stop()


def test_live_event_timestamp_cannot_reset_elapsed_order_rate_budget():
    broker, executor, ticks = setup_execution(elapsed_clock=lambda: 100.0)
    try:
        assert execute(executor, ticks).accepted
        result = execute(executor, ticks, signal_time=NOW + timedelta(days=1))
        assert not result.accepted
        assert "rate limit" in result.reason
        assert not result.order_ids
    finally:
        broker.stop()


def test_explicit_event_rate_clock_is_replay_only():
    broker, executor, ticks = setup_execution()
    try:
        result = execute(executor, ticks, rate_limit_time=NOW.timestamp())
        assert not result.accepted
        assert result.reason == "event rate-limit time is replay-only"
        assert broker.get_positions() == []
    finally:
        broker.stop()


def test_live_fresh_pair_reaches_real_simulated_fills():
    broker, executor, ticks = setup_execution()
    try:
        result = execute(executor, ticks)
        assert result.accepted, result.reason
        assert len(result.order_ids) == 2
        assert sum(p.long_total + p.short_total for p in broker.get_positions()) == 2
    finally:
        broker.stop()


def test_fresh_quotes_cannot_override_broker_snapshot_failure(monkeypatch):
    broker, executor, ticks = setup_execution()
    monkeypatch.setattr(broker, "health_error", lambda: "incomplete account snapshot")
    try:
        result = execute(executor, ticks)
        assert not result.accepted
        assert result.reason == "incomplete account snapshot"
        assert not result.order_ids
        assert broker.get_positions() == []
    finally:
        broker.stop()


def test_broker_failure_between_legs_preserves_filled_exposure(monkeypatch):
    broker, executor, ticks = setup_execution()
    calls = 0

    def health():
        nonlocal calls
        calls += 1
        return None if calls <= 2 else "account query identity mismatch"

    monkeypatch.setattr(broker, "health_error", health)
    try:
        result = execute(executor, ticks)
        assert not result.accepted
        assert len(result.order_ids) == 1
        assert "identity mismatch" in result.reason
        assert sum(p.long_total + p.short_total for p in broker.get_positions()) == 1
    finally:
        broker.stop()


def test_disconnect_during_sizing_is_rechecked_before_first_write(monkeypatch):
    broker, executor, ticks = setup_execution()
    calls = 0

    def ready():
        nonlocal calls
        calls += 1
        return calls == 1

    monkeypatch.setattr(broker, "is_ready", ready)
    try:
        result = execute(executor, ticks)
        assert not result.accepted
        assert not result.order_ids
        assert broker.get_positions() == []
    finally:
        broker.stop()
