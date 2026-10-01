from datetime import datetime, timezone

import pandas as pd

from afuture.directional import DirectionalConfig
from afuture.directional_runtime import DirectionalPortfolioManager
from afuture.execution_aligned_runtime import (
    ExecutionAlignedDirectionalPortfolioManager,
    ExecutionAlignedSignalHistory,
)
from afuture.models import (
    AccountSnapshot,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    Offset,
    OrderType,
    Tick,
)
from afuture.risk import RiskConfig, RiskManager

NOW = datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc)  # 21:01 China


def _tick(symbol: str, oi: float, *, depth: float = 1000.0) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=NOW,
        bid_price=99.0,
        ask_price=101.0,
        last_price=100.0,
        bid_volume=depth,
        ask_volume=depth,
        volume=20000,
        open_interest=oi,
        trading_day="20260825",
        limit_up=120.0,
        limit_down=80.0,
    )


class _Provider:
    def __init__(self, products=("A",)):
        dates = pd.date_range(end="2026-08-21", periods=180, freq="B")
        self.frame = pd.DataFrame(
            {product: range(100, 280) for product in products},
            index=dates,
            dtype=float,
        )
        self.calls = 0

    def load(self, products):
        self.calls += 1
        return self.frame[list(products)].copy()


class _Policy:
    def __init__(self, weights=None):
        self.weights = weights or {"A": 1.0}

    def target_weights(self, close):
        assert not close.empty
        return dict(self.weights)


class _ExecutionProvider:
    def __init__(self, products):
        dates = pd.date_range(end="2026-08-21", periods=180, freq="B")
        close = pd.DataFrame(
            {product: range(100, 280) for product in products}, index=dates, dtype=float
        )
        self.history = ExecutionAlignedSignalHistory(close.shift(1).fillna(close.iloc[0]), close)

    def load(self, products):
        return self.history


class _ExecutionPolicy:
    def __init__(self, weights):
        self.weights = dict(weights)

    def target_weights(self, open_prices, close):
        assert open_prices.index.equals(close.index)
        return dict(self.weights)


class _Broker:
    metadata_query_blocks = False

    def __init__(self, *, depth=1000.0, include_m=False):
        self.catalog = [
            ContractInfo("A2609", "DCE", "A", "2026-09-15"),
            ContractInfo("A2611", "DCE", "A", "2026-11-15"),
        ]
        if include_m:
            self.catalog.append(ContractInfo("M2609", "DCE", "M", "2026-09-15"))
        symbols = [item.symbol for item in self.catalog]
        self.specs = {symbol: ContractSpec(symbol, "DCE", 10, 1, 0.1, 0.1) for symbol in symbols}
        self.positions = [ContractPosition("A2609", "DCE", long_today=2, long_price=100.0)]
        self.orders = []
        self.active_orders = []
        self.subscriptions = []
        self.ticks = {
            "A2609": _tick("A2609", 10000, depth=depth),
            "A2611": _tick("A2611", 20000, depth=depth),
        }
        if include_m:
            self.ticks["M2609"] = _tick("M2609", 20000, depth=depth)
        self.account = AccountSnapshot(
            balance=100000,
            equity=100000,
            available=100000,
            margin=0,
            realized_pnl=0,
            unrealized_pnl=0,
            trading_day="20260825",
        )

    def is_ready(self):
        return True

    def get_contract_catalog(self):
        return list(self.catalog)

    def subscribe(self, symbol, exchange):
        self.subscriptions.append((symbol, exchange))

    def get_live_contract_specs(self, symbols, timeout_seconds=10.0):
        return {symbol: self.specs[symbol] for symbol in symbols}

    def get_account(self):
        return self.account

    def get_positions(self):
        return list(self.positions)

    def get_active_orders(self):
        return list(self.active_orders)

    def send_order(self, request):
        self.orders.append(request)
        return f"order-{len(self.orders)}"

    def cancel_order(self, order_id):
        pass


def _risk():
    return RiskManager(
        RiskConfig(
            max_margin_ratio=0.50,
            min_available_ratio=0.20,
            max_contract_volume=5,
            min_depth_multiple=2.0,
            open_cooldown_minutes=0,
            close_blackout_minutes=0,
        )
    )


def _manager(broker, *, products=("A",), weights=None):
    config = DirectionalConfig(
        enabled=True,
        policy="execution_aligned",
        products=products,
        exchanges=("DCE",),
        max_gross_leverage=2.0,
        max_contract_volume=5,
        rebalance_window="21:00-21:10",
        signal_max_age_hours=120.0,
    )
    return DirectionalPortfolioManager(
        config,
        broker,
        _risk(),
        historical_mode=True,
        signal_provider=_Provider(products),
        policy=_Policy(weights),
        aggressive_ticks=1,
    )


def _execution_manager(broker, *, products, weights):
    config = DirectionalConfig(
        enabled=True,
        policy="execution_aligned",
        products=products,
        exchanges=("DCE",),
        max_gross_leverage=2.0,
        max_contract_volume=5,
        rebalance_window="21:00-21:10",
        signal_max_age_hours=120.0,
    )
    return ExecutionAlignedDirectionalPortfolioManager(
        config,
        broker,
        _risk(),
        historical_mode=True,
        signal_provider=_ExecutionProvider(products),
        policy=_ExecutionPolicy(weights),
        activity_tracker=None,
        aggressive_ticks=1,
    )


def test_manager_subscribes_universe_and_reduces_before_opening_new_main_contract():
    broker = _Broker()
    manager = _manager(broker)
    manager.bootstrap(NOW)
    assert set(broker.subscriptions) == {("A2609", "DCE"), ("A2611", "DCE")}
    for tick in broker.ticks.values():
        manager.observe(tick)

    assert manager.required_symbols() == {"A2609"}

    result = manager.maybe_rebalance(NOW)
    assert result.action == "reduce"
    assert broker.orders
    assert all(order.offset is not Offset.OPEN for order in broker.orders)
    assert all(order.order_type is OrderType.FAK for order in broker.orders)
    assert broker.orders[0].symbol == "A2609"

    broker.positions = []
    broker.orders.clear()
    assert manager.required_symbols() == set()
    result = manager.maybe_rebalance(NOW)
    assert result.action == "open"
    assert broker.orders
    assert all(order.offset is Offset.OPEN for order in broker.orders)
    assert broker.orders[0].symbol == "A2611"
    assert broker.orders[0].volume == 5


def test_manager_realized_gross_guard_only_reduces_after_actual_two_x_breach():
    broker = _Broker()
    broker.positions = [ContractPosition("A2609", "DCE", long_today=250, long_price=100.0)]
    manager = _manager(broker)
    manager.bootstrap(NOW)
    manager.observe(broker.ticks["A2609"])

    result = manager.enforce_realized_gross_limit(NOW)

    assert result.action == "reduce"
    assert len(broker.orders) == 1
    order = broker.orders[0]
    assert order.symbol == "A2609"
    assert order.volume == 50
    assert order.offset is not Offset.OPEN
    assert order.order_type is OrderType.FAK
    assert order.reference == "directional:gross-guard"


def test_manager_realized_gross_guard_does_not_preemptively_haircut_two_x():
    broker = _Broker()
    broker.positions = [ContractPosition("A2609", "DCE", long_today=200, long_price=100.0)]
    manager = _manager(broker)
    manager.bootstrap(NOW)
    manager.observe(broker.ticks["A2609"])

    result = manager.enforce_realized_gross_limit(NOW)

    assert result.action == "hold"
    assert broker.orders == []


def test_manager_realized_gross_guard_waits_for_active_orders_instead_of_duplicating_reductions():
    broker = _Broker()
    broker.positions = [ContractPosition("A2609", "DCE", long_today=250, long_price=100.0)]
    broker.active_orders = [object()]
    manager = _manager(broker)
    manager.bootstrap(NOW)
    manager.observe(broker.ticks["A2609"])

    result = manager.enforce_realized_gross_limit(NOW)

    assert result.action == "wait"
    assert broker.orders == []


def test_missing_new_target_cannot_block_unrelated_reduction():
    broker = _Broker()
    manager = _execution_manager(broker, products=("A", "M"), weights={"M": 1.0})
    manager.bootstrap(NOW)
    for tick in broker.ticks.values():
        manager.observe(tick)

    result = manager.maybe_rebalance(NOW)
    assert result.action == "reduce"
    assert [order.symbol for order in broker.orders] == ["A2609"]
    assert all(order.offset is not Offset.OPEN for order in broker.orders)


def test_missing_target_with_existing_same_product_freezes_it_while_reducing_other_risk():
    broker = _Broker(include_m=True)
    broker.positions = [
        ContractPosition("A2609", "DCE", long_today=2, long_price=100.0),
        ContractPosition("M2609", "DCE", long_today=3, long_price=100.0),
    ]
    broker.ticks.pop("M2609")
    manager = _execution_manager(broker, products=("A", "M"), weights={"M": 1.0})
    manager.bootstrap(NOW)
    for tick in broker.ticks.values():
        manager.observe(tick)

    result = manager.maybe_rebalance(NOW)
    assert result.action == "reduce"
    assert [order.symbol for order in broker.orders] == ["A2609"]


def test_manager_fails_closed_when_opening_quote_depth_is_insufficient():
    broker = _Broker(depth=1.0)
    broker.positions = []
    manager = _manager(broker)
    manager.bootstrap(NOW)
    for tick in broker.ticks.values():
        manager.observe(tick)

    result = manager.maybe_rebalance(NOW)
    assert result.action == "reject"
    assert "depth" in result.reason
    assert broker.orders == []


def test_manager_flatten_only_emits_reducing_fak_orders():
    broker = _Broker()
    manager = _manager(broker)
    manager.bootstrap(NOW)
    for tick in broker.ticks.values():
        manager.observe(tick)

    result = manager.flatten(NOW)
    assert result.action == "reduce"
    assert broker.orders
    assert all(order.offset is not Offset.OPEN for order in broker.orders)
    assert all(order.order_type is OrderType.FAK for order in broker.orders)


def test_manager_flatten_closes_both_sides_when_same_contract_is_hedged():
    broker = _Broker()
    broker.positions = [
        ContractPosition(
            "A2609",
            "DCE",
            long_today=3,
            short_today=3,
            long_price=100.0,
            short_price=100.0,
        )
    ]
    manager = _manager(broker)
    manager.bootstrap(NOW)
    manager.observe(broker.ticks["A2609"])

    result = manager.flatten(NOW)

    assert result.action == "reduce"
    assert len(broker.orders) == 2
    assert {order.side.value for order in broker.orders} == {"BUY", "SELL"}
    assert {order.volume for order in broker.orders} == {3}
    assert all(order.offset is not Offset.OPEN for order in broker.orders)
    assert all(order.order_type is OrderType.FAK for order in broker.orders)
    assert all(order.reference == "directional:flatten" for order in broker.orders)


def test_directional_opening_uses_best_quote_when_depth_covers_order_but_reduction_stays_aggressive():
    broker = _Broker(depth=1000.0)
    manager = _manager(broker)
    manager.bootstrap(NOW)
    for tick in broker.ticks.values():
        manager.observe(tick)

    reduction = manager.maybe_rebalance(NOW)
    assert reduction.action == "reduce"
    assert broker.orders[0].side.value == "SELL"
    assert broker.orders[0].price == 98.0

    broker.positions = []
    broker.orders.clear()
    opening = manager.maybe_rebalance(NOW)
    assert opening.action == "open"
    assert broker.orders[0].side.value == "BUY"
    assert broker.orders[0].price == 101.0
    assert broker.orders[0].order_type is OrderType.FAK
