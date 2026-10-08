from __future__ import annotations

import importlib.util
from datetime import datetime, timezone

from afuture.models import (
    ContractInfo,
    ContractSpec,
    FeeSpec,
    Offset,
    OrderRequest,
    OrderSide,
    Tick,
)


def spec(symbol: str, exchange: str = "DCE") -> ContractSpec:
    return ContractSpec(
        symbol,
        exchange,
        10,
        1,
        0.10,
        0.10,
        FeeSpec(open_fixed=1.0, close_fixed=1.0),
    )


def tick(
    symbol: str,
    when: datetime,
    mid: float,
    *,
    spread: float = 1.0,
    volume: float = 20000,
    oi: float = 80000,
    trading_day: str = "20260821",
) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=when,
        bid_price=mid - spread / 2,
        ask_price=mid + spread / 2,
        last_price=mid,
        bid_volume=100,
        ask_volume=100,
        trading_day=trading_day,
        volume=volume,
        open_interest=oi,
    )


def catalog() -> list[ContractInfo]:
    return [
        ContractInfo("m2609", "DCE", "m", "2026-09-15"),
        ContractInfo("m2701", "DCE", "m", "2027-01-15"),
    ]


def test_shadow_broker_never_sends_order_to_live_broker():
    assert importlib.util.find_spec("afuture.broker.shadow") is not None
    from afuture.broker.shadow import ShadowBroker

    class LiveBroker:
        def __init__(self):
            self.sent = 0

        def start(self):
            pass

        def stop(self):
            pass

        def is_ready(self):
            return True

        def subscribe(self, symbol, exchange):
            pass

        def send_order(self, request):
            self.sent += 1
            raise AssertionError("shadow must never delegate send_order")

        def get_contract_catalog(self):
            return catalog()

        def get_live_contract_specs(self, symbols, timeout_seconds=10.0):
            return {symbol: spec(symbol) for symbol in symbols}

        def poll_events(self):
            return []

        def get_account(self):
            from afuture.models import AccountSnapshot

            return AccountSnapshot(500000, 500000, 500000, 0, 0, 0, "20260821")

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def get_order(self, order_id):
            return None

        def cancel_order(self, order_id):
            pass

        def get_trading_day(self):
            return "20260821"

        def health_error(self):
            return None

    live = LiveBroker()
    shadow = ShadowBroker(live, 500000)
    shadow.start()
    try:
        shadow.update_specs({"m2609": spec("m2609")})
        now = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
        shadow.publish_tick(tick("m2609", now, 3000))
        order_id = shadow.send_order(
            OrderRequest("m2609", "DCE", OrderSide.BUY, Offset.OPEN, 1, 3001)
        )
        assert order_id.startswith("SIM-")
        assert live.sent == 0
    finally:
        shadow.stop()
