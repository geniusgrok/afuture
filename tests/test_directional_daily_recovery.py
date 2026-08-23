from datetime import datetime, timezone

from afuture.directional_engine import DirectionalTradingEngine
from afuture.directional_runtime import DirectionalActionResult
from afuture.models import AccountSnapshot, RuntimeMode, Tick
from afuture.risk import RiskConfig, RiskManager
from afuture.state import StateStore


NOW = datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc)


class _Manager:
    def __init__(self):
        self.risk = False
        self.bootstrap_calls = 0
        self.flatten_calls = 0

    def bootstrap(self, now):
        self.bootstrap_calls += 1

    def observe(self, tick):
        pass

    def maybe_rebalance(self, now):
        return DirectionalActionResult("hold")

    def flatten(self, now):
        self.flatten_calls += 1
        return DirectionalActionResult("reduce" if self.risk else "hold")

    def has_risk(self):
        return self.risk

    def required_symbols(self):
        return set()

    def close(self):
        pass

    def directional_order_expectation(self, order_id):
        return None

    def _finalize_quality_cycle_if_settled(self, now):
        pass


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


def _tick(trading_day="20260825"):
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
        trading_day=trading_day,
        limit_up=120,
        limit_down=80,
    )


def _engine(tmp_path, risk):
    broker = _Broker()
    manager = _Manager()
    engine = DirectionalTradingEngine(
        broker,
        [],
        {},
        risk,
        StateStore(tmp_path / "state.json"),
        directional_manager=manager,
        health_clock=lambda: NOW,
    )
    engine.start()
    return broker, manager, engine


def _finish_reduce_only(manager, engine):
    manager.risk = False
    engine.run_once()
    assert engine.halted is True
    assert engine.state.runtime_mode == RuntimeMode.HALTED.value


def test_daily_loss_circuit_flattens_then_recovers_only_on_next_trading_day(tmp_path):
    broker, manager, engine = _engine(
        tmp_path,
        RiskManager(RiskConfig(max_daily_loss_ratio=0.05, max_total_drawdown_ratio=0.30)),
    )
    manager.risk = True
    broker.account = AccountSnapshot(
        balance=94000,
        equity=94000,
        available=94000,
        margin=0,
        realized_pnl=-6000,
        unrealized_pnl=0,
        trading_day="20260825",
    )

    engine.on_tick(_tick())
    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert engine.state.directional_daily_circuit_day == "20260825"

    _finish_reduce_only(manager, engine)
    engine._handle_account_event(broker.account)
    assert engine.halted is True

    broker.account = AccountSnapshot(
        balance=94000,
        equity=94000,
        available=94000,
        margin=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trading_day="20260826",
    )
    engine._handle_account_event(broker.account)

    assert engine.halted is False
    assert engine.state.runtime_mode == RuntimeMode.RUNNING.value
    assert engine.state.kill_switch is False
    assert engine.state.directional_daily_circuit_day == ""


def test_same_day_restart_bootstraps_directional_manager_when_next_day_recovers(tmp_path):
    broker, manager, engine = _engine(
        tmp_path,
        RiskManager(RiskConfig(max_daily_loss_ratio=0.05, max_total_drawdown_ratio=0.30)),
    )
    manager.risk = True
    broker.account = AccountSnapshot(
        balance=94000,
        equity=94000,
        available=94000,
        margin=0,
        realized_pnl=-6000,
        unrealized_pnl=0,
        trading_day="20260825",
    )
    engine.on_tick(_tick())
    _finish_reduce_only(manager, engine)
    engine.stop()

    restarted_broker = _Broker()
    restarted_broker.account = AccountSnapshot(
        balance=94000,
        equity=94000,
        available=94000,
        margin=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trading_day="20260825",
    )
    restarted_manager = _Manager()
    restarted = DirectionalTradingEngine(
        restarted_broker,
        [],
        {},
        RiskManager(RiskConfig(max_daily_loss_ratio=0.05, max_total_drawdown_ratio=0.30)),
        StateStore(tmp_path / "state.json"),
        directional_manager=restarted_manager,
        health_clock=lambda: NOW,
    )
    restarted.start()
    assert restarted.halted is True
    assert restarted_manager.bootstrap_calls == 0

    restarted_broker.account = AccountSnapshot(
        balance=94000,
        equity=94000,
        available=94000,
        margin=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trading_day="20260826",
    )
    restarted._handle_account_event(restarted_broker.account)
    restarted.run_once()

    assert restarted.halted is False
    assert restarted.state.runtime_mode == RuntimeMode.RUNNING.value
    assert restarted_manager.bootstrap_calls == 1
    assert restarted._directional_initialized is True


def test_drawdown_breach_remains_hard_halt_and_is_not_auto_recoverable(tmp_path):
    broker, manager, engine = _engine(
        tmp_path,
        RiskManager(RiskConfig(max_daily_loss_ratio=0.50, max_total_drawdown_ratio=0.05)),
    )
    manager.risk = True
    broker.account = AccountSnapshot(
        balance=94000,
        equity=94000,
        available=94000,
        margin=0,
        realized_pnl=-6000,
        unrealized_pnl=0,
        trading_day="20260825",
    )

    engine.on_tick(_tick())
    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert engine.state.directional_daily_circuit_day == ""

    _finish_reduce_only(manager, engine)
    broker.account = AccountSnapshot(
        balance=94000,
        equity=94000,
        available=94000,
        margin=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trading_day="20260826",
    )
    engine._handle_account_event(broker.account)

    assert engine.halted is True
    assert engine.state.runtime_mode == RuntimeMode.HALTED.value


def test_hard_drawdown_takes_precedence_when_daily_loss_is_also_breached(tmp_path):
    broker, manager, engine = _engine(
        tmp_path,
        RiskManager(RiskConfig(max_daily_loss_ratio=0.05, max_total_drawdown_ratio=0.30)),
    )
    manager.risk = True
    broker.account = AccountSnapshot(
        balance=60000,
        equity=60000,
        available=60000,
        margin=0,
        realized_pnl=-40000,
        unrealized_pnl=0,
        trading_day="20260825",
    )

    engine.on_tick(_tick())
    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert engine.state.directional_daily_circuit_day == ""

    _finish_reduce_only(manager, engine)
    broker.account = AccountSnapshot(
        balance=60000,
        equity=60000,
        available=60000,
        margin=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trading_day="20260826",
    )
    engine._handle_account_event(broker.account)

    assert engine.halted is True
    assert engine.state.runtime_mode == RuntimeMode.HALTED.value
