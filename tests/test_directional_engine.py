from datetime import datetime, timezone

from afuture.directional_engine import DirectionalTradingEngine
from afuture.directional_runtime import DirectionalActionResult
from afuture.models import (
    AccountSnapshot,
    RuntimeMode,
    Tick,
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
        self.completed_account_returns = []
        self.skipped_completed_account_days = set()
        self.verified_completed_account_transitions = {("20260824", "20260825")}

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

    def record_completed_account_return(
        self,
        trading_day,
        daily_return,
        *,
        completed_equity=None,
    ):
        del completed_equity
        if trading_day in self.skipped_completed_account_days:
            return False
        self.completed_account_returns.append((trading_day, daily_return))
        return True

    def completed_account_day_is_contiguous(self, old_day, new_day):
        return (old_day, new_day) in self.verified_completed_account_transitions


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
        self.critical_pending = False

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

    def has_pending_critical_events(self):
        return self.critical_pending


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


def test_directional_engine_halts_before_stress90_bootstrap_without_activation_marker(
    tmp_path,
):
    from afuture.directional_stress90_policy import STRESS90_POLICY

    manager = _Manager()
    manager.runtime_policy_id = STRESS90_POLICY.policy_id
    manager.runtime_policy_definition_digest = STRESS90_POLICY.policy_definition_digest

    _broker, manager, engine = _engine(tmp_path, manager=manager)

    assert engine.halted is True
    assert "explicit activation" in engine.state.kill_reason
    assert manager.bootstrap_calls == 0


def test_stress90_hard_daily_loss_uses_verified_prebalance_after_rollover(tmp_path):
    risk = RiskManager(
        RiskConfig(
            max_daily_loss_ratio=0.05,
            max_total_drawdown_ratio=0.30,
            min_available_ratio=0.25,
        )
    )
    broker, manager, engine = _engine(tmp_path, risk=risk)
    manager.runtime_policy_id = "directional.stress90"
    engine.state.trading_day = "20260824"
    engine.state.day_start_equity = 1_000_000.0
    engine.state.equity_high_watermark = 1_000_000.0
    engine.state.last_account_trading_day = "20260824"
    engine.state.last_account_equity = 1_000_000.0
    engine.state.last_account_cash_flow_verified = True
    engine.state.last_account_settlement_id = 10
    risk.set_day_start_equity(1_000_000.0, "20260824")
    risk.restore_high_watermark(1_000_000.0)
    first = AccountSnapshot(
        951_000.0,
        951_000.0,
        951_000.0,
        0.0,
        -49_000.0,
        0.0,
        "20260825",
        previous_settlement_equity=1_000_000.0,
        settlement_verified=True,
        settlement_id=11,
    )
    breached = AccountSnapshot(
        949_000.0,
        949_000.0,
        949_000.0,
        0.0,
        -51_000.0,
        0.0,
        "20260825",
        previous_settlement_equity=1_000_000.0,
        settlement_verified=True,
        settlement_id=11,
    )

    broker.account = first
    engine._handle_account_event(first)
    assert engine.halted is False
    broker.account = breached
    engine._handle_account_event(breached)

    assert engine.halted is True
    assert engine.state.kill_reason == "daily loss limit reached"


def test_stress90_tick_cannot_reset_daily_loss_before_settlement_roll_forward(tmp_path):
    risk = RiskManager(RiskConfig(max_daily_loss_ratio=0.05, max_total_drawdown_ratio=0.30))
    broker, manager, engine = _engine(tmp_path, risk=risk)
    manager.runtime_policy_id = "directional.stress90"
    manager.requires_explicit_settlement_roll_forward = True
    engine.state.trading_day = "20260824"
    engine.state.day_start_equity = 100_000.0
    risk.set_day_start_equity(100_000.0, "20260824")
    broker.account = AccountSnapshot(
        balance=94_000.0,
        equity=94_000.0,
        available=94_000.0,
        margin=0.0,
        realized_pnl=-6_000.0,
        unrealized_pnl=0.0,
        trading_day="20260825",
    )

    engine.on_tick(_tick())

    assert manager.observe_calls == []
    assert engine.halted
    assert engine.state.kill_switch
    assert engine.state.trading_day == "20260824"
    assert risk._day_start_equity == 100_000.0


def test_directional_account_risk_breach_reduces_existing_risk_instead_of_halting(tmp_path):
    risk = RiskManager(RiskConfig(max_daily_loss_ratio=0.05, max_total_drawdown_ratio=0.30))
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
