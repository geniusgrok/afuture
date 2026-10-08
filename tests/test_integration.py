from datetime import datetime, timedelta, timezone
from pathlib import Path

from afuture.broker.sim import SimBroker
from afuture.engine import TradingEngine
from afuture.execution import PairExecutor
from afuture.models import (
    ContractSpec,
    PairConfig,
    SignalAction,
    SpreadSignal,
    Tick,
)
from afuture.risk import RiskConfig, RiskManager
from afuture.state import StateStore


def tick(symbol, bid, ask, minute=0, *, depth=20, ts=None):
    return Tick(
        symbol,
        "DCE",
        ts or datetime(2026, 8, 21, 9, minute, tzinfo=timezone.utc),
        bid,
        ask,
        (bid + ask) / 2,
        depth,
        depth,
        "20260821",
    )


def setup_specs():
    return {s: ContractSpec(s, "DCE", 10, 1, 0.1, 0.1) for s in ("m2609", "m2701")}


def test_second_leg_failure_rolls_back_filled_first_leg():
    specs = setup_specs()

    class RejectSecond(SimBroker):
        def __init__(self):
            super().__init__(500000, specs)
            self.calls = 0

        def send_order(self, request):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("second leg rejected")
            return super().send_order(request)

    broker = RejectSecond()
    broker.start()
    near, far = tick("m2609", 3020, 3021), tick("m2701", 2999, 3000)
    broker.publish_tick(near)
    broker.publish_tick(far)
    executor = PairExecutor(broker, RiskManager(RiskConfig()), specs, historical_mode=True)
    pair = PairConfig("p", "m2609", "m2701", "DCE", 1)
    signal = SpreadSignal("p", SignalAction.SHORT_SPREAD, 3, near.timestamp, 20, 10, 5)
    result = executor.execute_signal(pair, signal, near, far, open_pair_count=0, spread_std=5)
    assert not result.accepted and broker.get_positions() == [] and broker.calls == 3


def test_live_wall_clock_detects_total_market_freeze(tmp_path: Path):
    specs = setup_specs()
    pair = PairConfig("p", "m2609", "m2701", "DCE", 1)
    broker = SimBroker(500000, specs)
    now = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    engine = TradingEngine(
        broker,
        [pair],
        specs,
        RiskManager(RiskConfig(max_quote_age_seconds=5)),
        StateStore(tmp_path / "s.json"),
        health_clock=lambda: now + timedelta(seconds=10),
    )
    engine.start()
    broker.publish_tick(tick("m2609", 3000, 3001, ts=now))
    broker.publish_tick(tick("m2701", 2990, 2991, ts=now))
    engine.run_once()
    assert engine.halted and "stale" in engine.state.kill_reason
