from datetime import datetime, timezone

from afuture.broker.sim import SimBroker
from afuture.models import ContractSpec, Offset, OrderRequest, OrderSide, OrderStatus, OrderType, Tick


def _spec() -> ContractSpec:
    return ContractSpec("RBX", "SHFE", 10, 1, 0.15, 0.15)


def _tick(*, bid_volume: float = 8, ask_volume: float = 8, limit_up: float = 0.0) -> Tick:
    return Tick(
        symbol="RBX",
        exchange="SHFE",
        timestamp=datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc),
        bid_price=99,
        ask_price=100,
        last_price=100,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        trading_day="20260824",
        limit_up=limit_up,
        volume=1000,
        open_interest=5000,
    )


def test_realistic_l1_stress_haircuts_depth_and_reports_fak_remainder_through_order_state():
    broker = SimBroker(
        500_000,
        {"RBX": _spec()},
        conservative=True,
        depth_haircut=0.75,
        size_impact_ticks=1,
    )
    broker.start()
    try:
        broker.publish_tick(_tick(ask_volume=8))
        order_id = broker.send_order(
            OrderRequest(
                "RBX",
                "SHFE",
                OrderSide.BUY,
                Offset.OPEN,
                10,
                105,
                OrderType.FAK,
                "directional:test",
            )
        )
        order = broker.get_order(order_id)
        assert order is not None
        assert order.traded == 6
        assert order.status is OrderStatus.CANCELLED
        assert order.request.volume - order.traded == 4
        trades = broker.get_trades()
        assert [(trade.volume, trade.price) for trade in trades] == [(6, 101)]
    finally:
        broker.stop()


def test_realistic_l1_size_impact_respects_daily_price_limit():
    broker = SimBroker(
        500_000,
        {"RBX": _spec()},
        conservative=True,
        depth_haircut=0.75,
        size_impact_ticks=2,
    )
    broker.start()
    try:
        broker.publish_tick(_tick(ask_volume=4, limit_up=100.5))
        broker.send_order(
            OrderRequest(
                "RBX",
                "SHFE",
                OrderSide.BUY,
                Offset.OPEN,
                8,
                105,
                OrderType.FAK,
                "directional:test",
            )
        )
        assert broker.get_trades()[0].price == 100.5
    finally:
        broker.stop()


def test_default_simulator_keeps_full_displayed_depth_and_no_dynamic_impact():
    broker = SimBroker(500_000, {"RBX": _spec()}, conservative=True)
    broker.start()
    try:
        broker.publish_tick(_tick(ask_volume=8))
        order_id = broker.send_order(
            OrderRequest(
                "RBX",
                "SHFE",
                OrderSide.BUY,
                Offset.OPEN,
                8,
                100,
                OrderType.FAK,
                "directional:test",
            )
        )
        order = broker.get_order(order_id)
        assert order is not None
        assert order.traded == 8
        assert order.status is OrderStatus.FILLED
        assert order.average_price == 100
    finally:
        broker.stop()


def test_execution_stress_summary_attributes_fill_spread_impact_latency_and_unfilled_quantity():
    broker = SimBroker(
        500_000,
        {"RBX": _spec()},
        conservative=True,
        latency_ticks=1,
        depth_haircut=0.75,
        size_impact_ticks=1,
    )
    broker.start()
    try:
        broker.publish_tick(_tick(ask_volume=8))
        broker.send_order(
            OrderRequest(
                "RBX",
                "SHFE",
                OrderSide.BUY,
                Offset.OPEN,
                10,
                105,
                OrderType.FAK,
                "directional:test",
            )
        )
        broker.publish_tick(_tick(ask_volume=8))
        summary = broker.get_execution_stress_summary()
        assert summary["requested_volume"] == 10
        assert summary["filled_volume"] == 6
        assert summary["unfilled_volume"] == 4
        assert summary["fill_ratio"] == 0.6
        assert summary["turnover_notional"] == 6060.0
        assert summary["spread_cost"] == 30.0
        assert summary["slippage_impact_cost"] == 60.0
        assert summary["volume_weighted_latency_ticks"] == 1.0
    finally:
        broker.stop()
