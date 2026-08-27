import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from afuture.broker.shadow import ShadowBroker
from afuture.broker.sim import SimBroker
from afuture.models import (
    BrokerEvent,
    ContractSpec,
    Offset,
    OrderRequest,
    OrderSide,
    OrderType,
    Tick,
)


def _tick(symbol: str, bid: float, ask: float) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc),
        bid_price=bid,
        ask_price=ask,
        last_price=(bid + ask) / 2,
        bid_volume=20,
        ask_volume=20,
        trading_day="20260821",
    )


def test_latency_fill_events_precede_tick_strategy_event():
    broker = SimBroker(
        500000,
        {"N": ContractSpec("N", "DCE", 10, 1, 0.15, 0.15)},
        conservative=True,
        latency_ticks=1,
    )
    broker.start()
    try:
        broker.publish_tick(_tick("N", 99, 100))
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
        # Drop the submission acknowledgement; the next market tick makes the
        # delayed order eligible and must update expected trade state before
        # that same tick can drive a fresh strategy decision.
        broker.poll_events()
        broker.publish_tick(_tick("N", 99, 100))
        events = broker.poll_events()
        assert [event.event_type for event in events] == ["trade", "order", "tick"]
        assert broker.get_positions()[0].long_total == 1
    finally:
        broker.stop()


class _ShadowLive:
    def __init__(self, trading_day: str = "20260821") -> None:
        self.trading_day = trading_day
        self.events: list[BrokerEvent] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def is_ready(self) -> bool:
        return self.started

    def get_trading_day(self) -> str:
        return self.trading_day

    def get_account_identity_digest(self) -> str:
        return sha256(b"shadow-causality-account").hexdigest()

    def poll_events(self) -> list[BrokerEvent]:
        events = list(self.events)
        self.events.clear()
        return events


def test_shadow_delayed_fill_precedes_trigger_tick_after_durable_batch(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "shadow-causality.json"
    live = _ShadowLive()
    broker = ShadowBroker(live, 500_000, latency_ticks=1, state_path=state_path)
    broker.update_specs({"N": ContractSpec("N", "DCE", 10, 1, 0.15, 0.15)})
    broker.start()
    first = _tick("N", 99, 100)
    broker.publish_tick(first)
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
    broker.sim.poll_events()
    trigger = _tick("N", 99, 100)
    live.events = [BrokerEvent("tick", trigger)]

    events = broker.poll_events()

    assert [event.event_type for event in events] == ["trade", "order", "tick"]
    assert events[-1].payload is trigger
    assert json.loads(state_path.read_text(encoding="utf-8"))["operation"] is None
    market_path = state_path.with_name(f"{state_path.name}.market")
    assert json.loads(market_path.read_text(encoding="utf-8"))["operation"] is None


def test_shadow_rollover_cancel_precedes_first_new_day_tick(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "shadow-rollover-causality.json"
    live = _ShadowLive()
    broker = ShadowBroker(live, 500_000, latency_ticks=10, state_path=state_path)
    broker.update_specs({"N": ContractSpec("N", "DCE", 10, 1, 0.15, 0.15)})
    broker.start()
    broker.publish_tick(_tick("N", 99, 100))
    order_id = broker.send_order(OrderRequest("N", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90))
    broker.sim.poll_events()
    live.trading_day = "20260822"
    trigger = Tick(
        **{
            **_tick("N", 99, 100).__dict__,
            "trading_day": "20260822",
        }
    )
    live.events = [BrokerEvent("tick", trigger)]

    events = broker.poll_events()

    assert [event.event_type for event in events] == ["order", "tick"]
    assert events[0].payload.order_id == order_id
    assert broker.owns_order(order_id)
    assert json.loads(state_path.read_text(encoding="utf-8"))["operation"] is None
    market_path = state_path.with_name(f"{state_path.name}.market")
    assert json.loads(market_path.read_text(encoding="utf-8"))["operation"] is None
