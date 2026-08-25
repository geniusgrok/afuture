from datetime import datetime, timezone

from afuture.models import Offset, Order, OrderRequest, OrderSide, OrderStatus, OrderType, Trade
from afuture.quality import ExecutionQualityRecorder

NOW = datetime(2026, 8, 25, 1, 1, tzinfo=timezone.utc)


def test_directional_quality_recorder_keeps_pair_summary_compatible_and_adds_directional(tmp_path):
    recorder = ExecutionQualityRecorder(tmp_path / "quality.jsonl")
    recorder.record_directional_rebalance(
        cycle_id="d-1",
        signal_day="2026-08-21",
        activity_day="20260821",
        target_gross=1.5,
        target_lots={"A2609": 3},
        reductions={},
        openings={"A2609": 3},
        planned_turnover_notional=30000.0,
        reason="rebalance",
    )
    recorder.record_directional_fill(
        cycle_id="d-1",
        order_id="o-1",
        product="A",
        symbol="A2609",
        side="BUY",
        offset="OPEN",
        expected_price=100.0,
        fill_price=100.2,
        volume=2,
        multiplier=10.0,
        slippage_bps=20.0,
        commission=3.0,
        commission_source="verified_fee_schedule",
        fill_notional=2004.0,
    )
    recorder.record_directional_fill(
        cycle_id="d-1",
        order_id="o-1",
        product="A",
        symbol="A2609",
        side="BUY",
        offset="OPEN",
        expected_price=100.0,
        fill_price=100.4,
        volume=1,
        multiplier=10.0,
        slippage_bps=40.0,
        commission=1.5,
        commission_source="verified_fee_schedule",
        fill_notional=1004.0,
    )
    recorder.record_directional_cycle(
        cycle_id="d-1",
        target_tracking_error=0.0,
        completion_latency_ms=2500.0,
        partial_count=1,
        rejected_count=0,
        realized_turnover_notional=3008.0,
    )

    summary = recorder.summary()
    assert summary["round_trips"] == 0
    directional = summary["directional"]
    assert directional["rebalance_events"] == 1
    assert directional["fill_count"] == 2
    assert directional["cycles"] == 1
    assert directional["turnover_notional"] == 3008.0
    assert directional["commission_total"] == 4.5
    assert directional["median_slippage_bps"] == 30.0
    assert directional["p95_slippage_bps"] == 40.0
    assert directional["median_tracking_error"] == 0.0
    assert directional["partial_count"] == 1
    assert directional["rejected_count"] == 0


def test_directional_order_expectation_uses_broker_fill_truth_without_position_side_effects(
    tmp_path,
):
    from afuture.directional import DirectionalConfig
    from afuture.directional_runtime import DirectionalPortfolioManager
    from afuture.models import AccountSnapshot, ContractSpec
    from afuture.risk import RiskConfig, RiskManager

    class Broker:
        def __init__(self):
            self.orders = {}
            self.positions = []
            self.account = AccountSnapshot(100000, 100000, 100000, 0, 0, 0, "20260825")

        def send_order(self, request):
            order_id = f"o-{len(self.orders) + 1}"
            self.orders[order_id] = Order(order_id, request, OrderStatus.NOT_TRADED)
            return order_id

        def get_active_orders(self):
            return [order for order in self.orders.values() if order.active]

        def get_positions(self):
            return list(self.positions)

        def get_account(self):
            return self.account

    broker = Broker()
    recorder = ExecutionQualityRecorder(tmp_path / "directional.jsonl")
    manager = DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, products=("A",), exchanges=("DCE",)),
        broker,
        RiskManager(RiskConfig()),
        signal_provider=object(),
        policy=object(),
        quality_recorder=recorder,
    )
    manager._specs["A2609"] = ContractSpec("A2609", "DCE", 10, 1, 0.1, 0.1)
    manager._start_quality_cycle(
        NOW,
        signal_day="2026-08-21",
        activity_day="20260821",
        target_gross=1.0,
        target_lots={"A2609": 2},
        reductions={},
        openings={"A2609": 2},
        planned_turnover_notional=2000.0,
        reason="test",
    )
    request = OrderRequest(
        "A2609", "DCE", OrderSide.BUY, Offset.OPEN, 2, 100.0, OrderType.FAK, "directional:A"
    )
    order_id = broker.send_order(request)
    manager._register_quality_order(order_id, request, manager._specs["A2609"], NOW)
    expectation = manager.directional_order_expectation(order_id)
    assert expectation["expected_price"] == 100.0
    assert expectation["multiplier"] == 10.0

    trade = Trade("t-1", order_id, "A2609", "DCE", OrderSide.BUY, Offset.OPEN, 2, 100.3, NOW)
    # Observability callbacks must not mutate Broker positions; TradingEngine remains position owner.
    manager.note_directional_quality_fill(trade, commission=2.0, commission_source="test")
    assert broker.positions == []
    rows = recorder._read()
    fill = [row for row in rows if row.get("event") == "directional_fill"][-1]
    assert abs(fill["slippage_bps"] - 30.0) < 1e-9
    assert fill["fill_notional"] == 2006.0

    broker.orders[order_id].status = OrderStatus.FILLED
    manager.note_directional_quality_order(broker.orders[order_id])
    manager._finalize_quality_cycle_if_settled(NOW)
    assert recorder.summary()["directional"]["cycles"] == 1
