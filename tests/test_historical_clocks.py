from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from afuture.broker.sim import SimBroker
from afuture.engine import TradingEngine
from afuture.execution import PairExecutor
from afuture.models import (
    ContractSpec,
    Offset,
    OrderRequest,
    OrderSide,
    OrderType,
    PairConfig,
    RuntimeMode,
    SignalAction,
    SpreadSignal,
    Tick,
)
from afuture.risk import RiskConfig, RiskManager
from afuture.state import StateStore


def _tick(symbol: str, ts: datetime, bid: float, ask: float) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=ts,
        bid_price=bid,
        ask_price=ask,
        last_price=(bid + ask) / 2,
        bid_volume=100,
        ask_volume=100,
        trading_day=ts.strftime("%Y%m%d"),
        volume=20000,
        open_interest=80000,
    )


def _specs():
    return {
        "N": ContractSpec("N", "DCE", 10, 1, 0.1, 0.1),
        "F": ContractSpec("F", "DCE", 10, 1, 0.1, 0.1),
    }


def test_pair_executor_can_use_market_event_time_for_historical_rate_limit():
    specs = _specs()
    broker = SimBroker(500000, specs)
    broker.start()
    try:
        risk = RiskManager(
            RiskConfig(
                max_orders_per_minute=2,
                min_depth_multiple=1.0,
                max_quote_age_seconds=30.0,
            )
        )
        executor = PairExecutor(
            broker, risk, specs, aggressive_ticks=0, slippage_ticks=0, historical_mode=True
        )
        pair = PairConfig("p", "N", "F", "DCE", 1)
        day1 = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
        near1, far1 = _tick("N", day1, 99, 100), _tick("F", day1, 89, 90)
        broker.publish_tick(near1)
        broker.publish_tick(far1)
        broker.poll_events()
        opened = executor.execute_signal(
            pair,
            SpreadSignal("p", SignalAction.LONG_SPREAD, -2.0, day1, 11, 20, 1),
            near1,
            far1,
            open_pair_count=0,
            spread_std=1,
            rate_limit_time=day1.timestamp(),
        )
        assert opened.accepted, opened.reason

        day2 = day1 + timedelta(days=1)
        near2, far2 = _tick("N", day2, 99, 100), _tick("F", day2, 89, 90)
        broker.publish_tick(near2)
        broker.publish_tick(far2)
        broker.poll_events()
        closed = executor.execute_signal(
            pair,
            SpreadSignal("p", SignalAction.EXIT, 0.0, day2, 11, 20, 1),
            near2,
            far2,
            open_pair_count=1,
            spread_std=1,
            rate_limit_time=day2.timestamp(),
        )
        assert closed.accepted, closed.reason
        assert broker.get_positions() == []
    finally:
        broker.stop()


def test_historical_legging_timeout_uses_market_event_time(tmp_path: Path):
    specs = _specs()
    pair = PairConfig("p", "N", "F", "DCE", 1)
    broker = SimBroker(500000, specs)
    engine = TradingEngine(
        broker,
        [pair],
        specs,
        RiskManager(RiskConfig(max_orders_per_minute=100)),
        StateStore(tmp_path / "state.json"),
        historical_mode=True,
        legging_timeout_seconds=2.0,
    )
    engine.start()
    try:
        start = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
        near = _tick("N", start, 99, 100)
        far = _tick("F", start, 89, 90)
        broker.publish_tick(near)
        broker.publish_tick(far)
        broker.poll_events()
        broker.send_order(
            OrderRequest(
                "N",
                "DCE",
                OrderSide.BUY,
                Offset.OPEN,
                1,
                100,
                OrderType.FAK,
                "p",
            )
        )
        broker.poll_events()
        assert broker.get_positions()[0].long_total == 1
        assert not engine.executor.pair_is_balanced(pair)

        engine.quotes = {"N": near, "F": far}
        engine._audit_pair_balance()
        assert engine.state.runtime_mode == RuntimeMode.RUNNING.value
        assert engine._imbalance_since["p"] == pytest.approx(start.timestamp())

        later = start + timedelta(seconds=3)
        engine.quotes = {
            "N": _tick("N", later, 99, 100),
            "F": _tick("F", later, 89, 90),
        }
        assert engine._imbalance_clock() == pytest.approx(later.timestamp())
        engine._audit_pair_balance()
        assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    finally:
        engine.stop()
