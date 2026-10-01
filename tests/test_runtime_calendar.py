"""Expected exchange sessions on real execution paths, not financial-day proof."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256

import pytest

from afuture.broker.sim import SimBroker
from afuture.config import AppConfig
from afuture.directional import DirectionalConfig
from afuture.directional_engine import DirectionalTradingEngine
from afuture.directional_runtime import DirectionalPortfolioManager
from afuture.engine import TradingEngine
from afuture.execution import PairExecutor
from afuture.models import (
    ContractInfo,
    ContractSpec,
    Offset,
    OrderRequest,
    OrderSide,
    PairConfig,
    SignalAction,
    SpreadSignal,
    Tick,
)
from afuture.risk import RiskConfig, RiskManager
from afuture.runtime_calendar import RuntimeCalendarError, RuntimeTradingCalendar
from afuture.runtime_factory import build_runtime_engine
from afuture.state import StateStore

CALENDAR = RuntimeTradingCalendar.load()


def stamp(local: str) -> datetime:
    return datetime.fromisoformat(local + "+08:00")


@pytest.mark.parametrize(
    "symbol,local,expected",
    [
        ("AU2612", "2026-09-18T21:01", "20260921"),
        ("AU2612", "2026-09-19T00:30", "20260921"),
        ("AU2612", "2026-09-19T02:30", None),
        ("AU2612", "2026-09-20T00:30", None),
        ("AU2612", "2026-09-20T09:01", None),  # statutory working Sunday, exchange closed
        ("CU2612", "2026-09-19T00:59", "20260921"),
        ("CU2612", "2026-09-19T01:00", None),
        ("RB2612", "2026-09-18T22:59", "20260921"),
        ("RB2612", "2026-09-18T23:00", None),
        ("HC2612", "2026-09-18T23:30", None),
        ("BU2612", "2026-09-18T23:30", None),
        ("AP701", "2026-09-18T21:01", None),
        ("AP701", "2026-09-21T09:01", "20260921"),
        ("M2701", "2026-09-24T21:01", None),
        ("AU2612", "2026-09-25T00:30", None),
        ("SC2612", "2026-09-28T09:01", "20260928"),
        ("SC2612", "2026-09-30T21:01", None),
        ("M2701", "2026-10-01T09:01", None),
        ("M2701", "2026-10-08T09:01", "20261008"),
        ("M2701", "2026-10-08T10:20", None),
        ("M2701", "2026-10-08T11:30", None),
        ("M2701", "2026-10-08T13:30", "20261008"),
        ("M2701", "2026-10-08T15:00", None),
        ("M2701", "2026-10-08T20:59", None),
        ("M2701", "2026-10-08T21:00", "20261009"),
    ],
)
def test_expected_calendar_sessions(symbol, local, expected):
    assert CALENDAR.expected_trading_day(symbol, stamp(local)) == expected


def test_weekend_and_holiday_are_not_missing_exchange_days():
    assert CALENDAR.next_trading_day("20260918", "SHFE") == "20260921"
    assert CALENDAR.next_trading_day("20260924", "DCE") == "20260928"
    assert CALENDAR.next_trading_day("20260930", "CZCE") == "20261008"
    with pytest.raises(RuntimeCalendarError, match="source is not"):
        CALENDAR.next_trading_day("20261001", "CZCE")


def test_unknown_expired_conflicting_calendar_cannot_authorize():
    risk = RiskManager(runtime_calendar=CALENDAR)
    for symbol, exchange, now, day in [
        ("M2701", "DCE", stamp("2026-09-18T21:01"), "20260918"),
        ("M2701", "SHFE", stamp("2026-09-18T21:01"), "20260921"),
        ("UNKNOWN", "DCE", stamp("2026-09-18T21:01"), "20260921"),
        ("M2701", "DCE", stamp("2027-01-04T09:01"), "20270104"),
        ("M2701", "DCE", datetime(2026, 9, 18, 21, 1), "20260921"),
    ]:
        assert not risk.check_runtime_session(symbol, exchange, now, day).allowed
    with pytest.raises(RuntimeCalendarError, match="exceeds"):
        CALENDAR.next_trading_day("20261231", "DCE")


def test_calendar_checks_duplicates_digest_and_partition_without_replacing_file(tmp_path):
    from pathlib import Path

    import afuture.runtime_calendar as module

    original = Path(module.__file__).with_name("runtime_calendar.json").read_bytes()
    location = tmp_path / "calendar.json"
    bad = json.loads(original)
    bad["payload"]["exchanges"]["DCE"]["open_days"].remove("2026-09-22")
    canonical = json.dumps(
        bad["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    bad["sha256"] = sha256(canonical).hexdigest()
    payload = json.dumps(bad).encode()
    location.write_bytes(payload)
    with pytest.raises(RuntimeCalendarError, match="incomplete"):
        RuntimeTradingCalendar.load(location)
    assert location.read_bytes() == payload
    location.write_text('{"payload":{},"payload":{},"sha256":"x"}')
    with pytest.raises(RuntimeCalendarError, match="duplicate"):
        RuntimeTradingCalendar.load(location)
    bad = json.loads(original)
    bad["sha256"] = "0" * 64
    location.write_text(json.dumps(bad))
    with pytest.raises(RuntimeCalendarError, match="checksum"):
        RuntimeTradingCalendar.load(location)


def pair_runtime(now, trading_day):
    specs = {s: ContractSpec(s, "SHFE", 10, 1, 0.1, 0.1) for s in ("RB2610", "RB2701")}
    broker = SimBroker(500000, specs)
    broker.start()
    ticks = tuple(
        Tick(
            s,
            "SHFE",
            now,
            bid,
            bid + 1,
            bid + 0.5,
            100,
            100,
            trading_day=trading_day,
            volume=20000,
            open_interest=80000,
        )
        for s, bid in (("RB2610", 99), ("RB2701", 89))
    )
    for tick in ticks:
        broker.publish_tick(tick)
    broker.poll_events()
    wall = [now]
    risk = RiskManager(RiskConfig(min_depth_multiple=1), runtime_calendar=CALENDAR)
    executor = PairExecutor(
        broker, risk, specs, aggressive_ticks=0, slippage_ticks=0, health_clock=lambda: wall[0]
    )
    return broker, executor, ticks, wall


def execute(executor, ticks):
    return executor.execute_signal(
        PairConfig("p", "RB2610", "RB2701", "SHFE", 1),
        SpreadSignal("p", SignalAction.LONG_SPREAD, -2.0, ticks[0].timestamp, 11, 20, 1),
        *ticks,
        open_pair_count=0,
        spread_std=1,
    )


@pytest.mark.parametrize(
    "local,day,allowed",
    [
        ("2026-09-18T21:01", "20260921", True),
        ("2026-09-18T21:01", "20260918", False),
        ("2026-09-24T21:01", "20260928", False),
        ("2026-09-18T23:01", "20260921", False),
    ],
)
def test_pair_production_send_path_intersects_calendar_and_counter(local, day, allowed):
    broker, executor, ticks, _ = pair_runtime(stamp(local), day)
    try:
        result = execute(executor, ticks)
        assert result.accepted is allowed, result.reason
        assert len(broker.get_trades()) == (2 if allowed else 0)
    finally:
        broker.stop()


def test_second_leg_and_rollback_cannot_trade_past_session_end():
    broker, executor, ticks, wall = pair_runtime(stamp("2026-09-18T22:59:59"), "20260921")
    send = broker.send_order

    def crossing(request):
        identity = send(request)
        wall[0] += timedelta(seconds=2)
        return identity

    broker.send_order = crossing
    try:
        result = execute(executor, ticks)
        assert not result.accepted and "session" in result.reason
        assert len(result.order_ids) == len(broker.get_trades()) == 1
        assert sum(p.long_total + p.short_total for p in broker.get_positions()) == 1
    finally:
        broker.stop()


def test_repair_cannot_override_calendar_or_position_validation():
    broker, executor, ticks, wall = pair_runtime(stamp("2026-09-18T22:59:59"), "20260921")
    broker.send_order(OrderRequest("RB2610", "SHFE", OrderSide.BUY, Offset.OPEN, 1, 100.0))
    wall[0] += timedelta(seconds=2)
    try:
        with pytest.raises(RuntimeError, match="session"):
            executor.flatten_imbalance(PairConfig("p", "RB2610", "RB2701", "SHFE", 1), *ticks)
        assert len(broker.get_trades()) == 1
    finally:
        broker.stop()


def test_span_smoke_reaches_real_execution_on_open_days_not_monthly_lifecycle_acceptance():
    # This tests the production calendar/order path over a month boundary. It is
    # intentionally NOT labeled account-continuity or unattended-month acceptance.
    opened = 0
    first = stamp("2026-09-18T09:01")
    for offset in range(45):
        now = first + timedelta(days=offset)
        expected = CALENDAR.expected_trading_day("RB2701", now)
        broker, executor, ticks, _ = pair_runtime(now, now.strftime("%Y%m%d"))
        try:
            result = execute(executor, ticks)
            assert result.accepted is (expected is not None), (now, result.reason)
            assert len(broker.get_trades()) == (2 if expected else 0)
            opened += int(result.accepted)
        finally:
            broker.stop()
    assert opened == 25


def test_runtime_factory_binds_calendar_but_replay_preserves_frozen_research(tmp_path):
    config = AppConfig("live", 100000, {}, [], RiskConfig(), None)
    broker = SimBroker(100000, {})
    live = build_runtime_engine(config, broker, StateStore(tmp_path / "live.json"))
    assert live.risk_manager.runtime_calendar.digest == CALENDAR.digest
    replay = build_runtime_engine(
        config, broker, StateStore(tmp_path / "replay.json"), historical_mode=True
    )
    assert replay.risk_manager.runtime_calendar is None
    offline = build_runtime_engine(
        replace(config, mode="replay"), broker, StateStore(tmp_path / "offline.json")
    )
    assert offline.risk_manager.runtime_calendar is None


def test_pair_health_does_not_call_scheduled_recess_stale(tmp_path):
    broker, executor, ticks, wall = pair_runtime(stamp("2026-09-18T09:01"), "20260918")
    engine = TradingEngine(
        broker,
        [PairConfig("p", "RB2610", "RB2701", "SHFE", 1)],
        executor.specs,
        executor.risk_manager,
        StateStore(tmp_path / "s.json"),
        health_clock=lambda: wall[0],
    )
    engine.quotes = {t.symbol: t for t in ticks}
    try:
        wall[0] = stamp("2026-09-18T12:00")
        assert engine._market_health_reason() == ""
        wall[0] = stamp("2026-09-18T13:31")
        assert "stale" in engine._market_health_reason()
        wall[0] = stamp("2027-01-04T09:01")
        assert "expired" in engine._market_health_reason()
    finally:
        broker.stop()


def test_directional_health_filters_calendar_without_changing_risk_owner(tmp_path):
    now = stamp("2026-09-18T09:01")
    specs = {"M2701": ContractSpec("M2701", "DCE", 10, 1, 0.1, 0.1)}
    catalog = [ContractInfo("M2701", "DCE", "M", "2027-01-15")]
    broker = SimBroker(100000, specs, contract_catalog=catalog)
    broker.start()
    risk = RiskManager(runtime_calendar=CALENDAR)
    manager = DirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True, policy="execution_aligned", products=("M",), exchanges=("DCE",)
        ),
        broker,
        risk,
        static_specs=specs,
    )
    manager.bootstrap(now)
    tick = Tick(
        "M2701",
        "DCE",
        now,
        99.0,
        101.0,
        100.0,
        1000,
        1000,
        trading_day="20260918",
        volume=20000,
        open_interest=20000,
    )
    broker.publish_tick(tick)
    manager.observe(tick)
    broker.send_order(OrderRequest("M2701", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))
    wall = [stamp("2026-09-18T12:00")]
    engine = DirectionalTradingEngine(
        broker,
        [],
        specs,
        risk,
        StateStore(tmp_path / "directional.json"),
        directional_manager=manager,
        health_clock=lambda: wall[0],
    )
    engine.quotes = {"M2701": tick}
    try:
        assert engine._market_health_reason() == ""
        wall[0] = stamp("2026-09-18T13:31")
        assert "stale" in engine._market_health_reason()
        request = OrderRequest("M2701", "DCE", OrderSide.SELL, Offset.CLOSE_TODAY, 1, 99.0)
        wall[0] = stamp("2026-09-24T21:01")
        current = replace(tick, timestamp=wall[0], trading_day="20260928")
        broker.publish_tick(current)
        with pytest.raises(RuntimeError, match="session"):
            manager._require_current_order_evidence(request, current, specs["M2701"], wall[0])
    finally:
        manager.close()
        broker.stop()


def test_flat_manager_waits_outside_sessions_before_creating_first_entry_intents():
    now = stamp("2026-09-18T20:59")
    specs = {"M2701": ContractSpec("M2701", "DCE", 10, 1, 0.1, 0.1)}
    broker = SimBroker(
        100000, specs, contract_catalog=[ContractInfo("M2701", "DCE", "M", "2027-01-15")]
    )
    broker.start()
    manager = DirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True, policy="execution_aligned", products=("M",), exchanges=("DCE",)
        ),
        broker,
        RiskManager(runtime_calendar=CALENDAR),
        static_specs=specs,
    )
    try:
        manager.bootstrap(now)
        assert not manager.required_symbols()
        assert not manager.has_active_runtime_session(now)
        assert manager.has_active_runtime_session(now + timedelta(minutes=1))
        assert not manager.has_active_runtime_session(stamp("2026-09-24T21:01"))
        assert not broker.get_orders()
    finally:
        manager.close()
        broker.stop()


def test_heartbeat_uses_same_session_filter_as_trading_risk():
    from types import SimpleNamespace

    from afuture.runtime_heartbeat import RuntimeHeartbeatObserver

    now = stamp("2026-09-18T12:00")
    tick = Tick(
        "RB2701",
        "SHFE",
        stamp("2026-09-18T11:29"),
        99.0,
        101.0,
        100.0,
        100,
        100,
        trading_day="20260918",
    )
    # Exercise the installed observer's method without an unrelated state-store setup.
    observer = RuntimeHeartbeatObserver.__new__(RuntimeHeartbeatObserver)
    wall = [now]
    observer.engine = SimpleNamespace(
        quotes={tick.symbol: tick},
        risk_manager=RiskManager(runtime_calendar=CALENDAR),
        health_clock=lambda: wall[0],
    )
    assert observer._quote_age() == 0
    wall[0] = stamp("2026-09-18T13:31")
    assert observer._quote_age() == 7320
