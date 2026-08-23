from datetime import datetime, timezone

from afuture.directional_engine import DirectionalTradingEngine
from afuture.directional_runtime import DirectionalActionResult
from afuture.models import (
    AccountSnapshot,
    Offset,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    RuntimeMode,
    Tick,
    Trade,
)
from afuture.risk import RiskConfig, RiskManager
from afuture.state import StateStore


NOW = datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc)


class _Manager:
    def __init__(self):
        self.bootstrap_calls = 0
        self.observe_calls = []
        self.rebalance_calls = 0
        self.flatten_calls = 0
        self.gross_guard_calls = 0
        self.risk = False
        self.closed = False
        self.next_result = DirectionalActionResult("hold")
        self.next_gross_guard_result = DirectionalActionResult("hold")
        self.quality_expectations = {}
        self.quality_orders = []
        self.quality_fills = []
        self.quality_finalize_calls = 0

    def bootstrap(self, now):
        self.bootstrap_calls += 1

    def observe(self, tick):
        self.observe_calls.append(tick)

    def maybe_rebalance(self, now):
        self.rebalance_calls += 1
        return self.next_result

    def enforce_realized_gross_limit(self, now):
        self.gross_guard_calls += 1
        return self.next_gross_guard_result

    def flatten(self, now):
        self.flatten_calls += 1
        return DirectionalActionResult("reduce" if self.risk else "hold")

    def has_risk(self):
        return self.risk

    def required_symbols(self):
        return set()

    def close(self):
        self.closed = True

    def directional_order_expectation(self, order_id):
        return self.quality_expectations.get(order_id)

    def note_directional_quality_order(self, order):
        self.quality_orders.append(order)

    def note_directional_quality_fill(self, trade, **kwargs):
        self.quality_fills.append((trade, kwargs))

    def _finalize_quality_cycle_if_settled(self, now):
        self.quality_finalize_calls += 1


class _Broker:
    def __init__(self):
        self.ready = False
        self.account = AccountSnapshot(
            balance=100000,
            equity=100000,
            available=100000,
            margin=0,
            realized_pnl=0,
            unrealized_pnl=0,
            trading_day="20260825",
        )

    def start(self):
        self.ready = True

    def stop(self):
        self.ready = False

    def is_ready(self):
        return self.ready

    def subscribe(self, symbol, exchange):
        pass

    def get_account(self):
        return self.account

    def get_positions(self):
        return []

    def get_active_orders(self):
        return []

    def poll_events(self):
        return []

    def health_error(self):
        return None

    def owns_order(self, order_id):
        return True


def _tick():
    return Tick(
        symbol="A2609",
        exchange="DCE",
        timestamp=NOW,
        bid_price=99,
        ask_price=101,
        last_price=100,
        bid_volume=100,
        ask_volume=100,
        volume=10000,
        open_interest=20000,
        trading_day="20260825",
        limit_up=120,
        limit_down=80,
    )


def _engine(tmp_path, manager=None, risk=None):
    broker = _Broker()
    manager = manager or _Manager()
    engine = DirectionalTradingEngine(
        broker,
        [],
        {},
        risk or RiskManager(RiskConfig()),
        StateStore(tmp_path / "state.json"),
        directional_manager=manager,
        health_clock=lambda: NOW,
    )
    engine.start()
    return broker, manager, engine


def test_directional_engine_forwards_ticks_enforces_gross_and_runs_manager(tmp_path):
    broker, manager, engine = _engine(tmp_path)
    assert manager.bootstrap_calls == 1

    tick = _tick()
    engine.on_tick(tick)
    assert manager.observe_calls == [tick]
    assert manager.gross_guard_calls == 1

    engine.run_once()
    assert manager.rebalance_calls == 1
    engine.stop()
    assert manager.closed is True


def test_directional_gross_guard_reduction_keeps_engine_running(tmp_path):
    _, manager, engine = _engine(tmp_path)
    manager.risk = True
    manager.next_gross_guard_result = DirectionalActionResult(
        "reduce", order_ids=("gross-guard-1",)
    )

    engine.on_tick(_tick())

    assert manager.gross_guard_calls == 1
    assert engine.state.runtime_mode == RuntimeMode.RUNNING.value
    assert engine.halted is False


def test_directional_gross_guard_reject_enters_fail_closed_reduce_only(tmp_path):
    _, manager, engine = _engine(tmp_path)
    manager.risk = True
    manager.next_gross_guard_result = DirectionalActionResult(
        "reject", "realized gross guard could not reduce"
    )

    engine.on_tick(_tick())

    assert manager.gross_guard_calls == 1
    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert engine.halted is False


def test_directional_signal_risk_off_enters_reduce_only_only_when_risk_exists(tmp_path):
    _, manager, engine = _engine(tmp_path)
    manager.risk = True
    manager.next_result = DirectionalActionResult(
        "risk_off", "required signal trading day unavailable"
    )
    engine.run_once()
    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert engine.halted is False

    _, flat_manager, flat_engine = _engine(tmp_path / "flat")
    flat_manager.risk = False
    flat_manager.next_result = DirectionalActionResult(
        "risk_off", "required signal trading day unavailable"
    )
    flat_engine.run_once()
    assert flat_engine.state.runtime_mode == RuntimeMode.RUNNING.value
    assert flat_engine.halted is False


def test_directional_account_risk_breach_reduces_existing_risk_instead_of_halting(tmp_path):
    risk = RiskManager(
        RiskConfig(max_daily_loss_ratio=0.05, max_total_drawdown_ratio=0.30)
    )
    broker, manager, engine = _engine(tmp_path, risk=risk)
    manager.risk = True
    broker.account = AccountSnapshot(
        balance=90000,
        equity=90000,
        available=90000,
        margin=0,
        realized_pnl=-10000,
        unrealized_pnl=0,
        trading_day="20260825",
    )
    engine.on_tick(_tick())
    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert engine.halted is False


def test_directional_reduce_only_flattens_before_halting(tmp_path):
    broker, manager, engine = _engine(tmp_path)
    manager.risk = True
    engine.enter_reduce_only("directional test")
    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value

    engine.run_once()
    assert manager.flatten_calls >= 1
    assert engine.halted is False

    manager.risk = False
    engine.run_once()
    assert engine.halted is True
    assert engine.state.runtime_mode == RuntimeMode.HALTED.value
    engine.stop()


def test_directional_engine_records_broker_order_and_trade_callbacks_after_position_truth(tmp_path):
    _, manager, engine = _engine(tmp_path)
    request = OrderRequest(
        symbol="A2609",
        exchange="DCE",
        side=OrderSide.BUY,
        offset=Offset.OPEN,
        volume=1,
        price=100.0,
        order_type=OrderType.FAK,
        reference="directional:A",
    )
    manager.quality_expectations["o-1"] = {
        "expected_price": 100.0,
        "multiplier": 10.0,
    }
    order = Order(
        "o-1",
        request,
        status=OrderStatus.FILLED,
        traded=1,
        average_price=100.2,
    )
    engine._handle_order_event(order)
    assert manager.quality_orders == [order]
    # Terminal order status must not finalize the cycle before its Trade callback.
    assert manager.quality_finalize_calls == 0

    trade = Trade(
        "t-1",
        "o-1",
        "A2609",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100.2,
        NOW,
        commission=1.5,
    )
    engine._handle_trade_event(trade)

    # Base TradingEngine remains the only expected-position owner.
    positions = engine.state_store.positions_from_state(engine.state)
    assert len(positions) == 1
    assert positions[0].symbol == "A2609"
    assert positions[0].long_total == 1
    assert manager.quality_fills
    recorded_trade, kwargs = manager.quality_fills[-1]
    assert recorded_trade == trade
    assert kwargs["commission"] == 1.5
    assert kwargs["commission_source"] == "broker_trade"
    assert manager.quality_finalize_calls == 1
