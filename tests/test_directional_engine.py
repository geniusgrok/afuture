from datetime import datetime, timezone

import pytest

from afuture.directional_engine import DirectionalTradingEngine
from afuture.directional_runtime import DirectionalActionResult
from afuture.models import (
    AccountSnapshot,
    BrokerEvent,
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


def test_directional_engine_forwards_ticks_enforces_gross_and_runs_manager(tmp_path):
    broker, manager, engine = _engine(tmp_path)
    assert manager.bootstrap_calls == 1

    tick = _tick()
    engine.on_tick(tick)
    assert manager.observe_calls == [tick]
    assert manager.gross_guard_calls == 0

    engine.run_once()
    assert manager.gross_guard_calls == 1
    assert manager.rebalance_calls == 1
    engine.stop()
    assert manager.closed is True


def test_directional_engine_never_rebalances_while_critical_fifo_has_backlog(tmp_path):
    broker, manager, engine = _engine(tmp_path)
    broker.critical_pending = True

    engine.run_once()

    assert manager.rebalance_calls == 0
    broker.critical_pending = False
    engine.run_once()
    assert manager.rebalance_calls == 1


def test_directional_engine_never_dispatches_gross_guard_after_poll_race(tmp_path):
    broker, manager, engine = _engine(tmp_path)

    def poll_with_concurrent_fatal_event():
        broker.critical_pending = True
        return [BrokerEvent("tick", _tick())]

    broker.poll_events = poll_with_concurrent_fatal_event

    engine.run_once()

    assert manager.observe_calls == [_tick()]
    assert manager.gross_guard_calls == 0
    assert manager.rebalance_calls == 0


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


def test_engine_persistence_preserves_account_bound_directional_policy_identity(tmp_path):
    marker = {
        "policy_id": "directional.stress90",
        "policy_definition_digest": "a" * 64,
        "products_manifest_digest": "b" * 64,
        "bootstrap_seed_digest": "c" * 64,
        "account_identity_digest": "d" * 64,
        "operator_reason": "commissioned",
    }
    store = StateStore(tmp_path / "state.json")
    from afuture.state import RuntimeState

    store.save(RuntimeState(strategy_states={"directional_policy_identity": marker}))
    broker = _Broker()
    manager = _Manager()
    engine = DirectionalTradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        store,
        directional_manager=manager,
        health_clock=lambda: NOW,
    )
    engine.start()
    engine._persist()

    assert store.load().strategy_states["directional_policy_identity"] == marker


def test_directional_engine_records_completed_broker_day_before_advancing_runtime_state(
    tmp_path,
):
    broker, manager, engine = _engine(tmp_path)
    engine.state.trading_day = "20260824"
    engine.state.day_start_equity = 100_000.0
    engine.state.last_account_trading_day = "20260824"
    engine.state.last_account_equity = 90_000.0
    next_account = AccountSnapshot(
        90_000.0,
        90_000.0,
        90_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
        previous_settlement_equity=90_000.0,
        settlement_verified=True,
    )

    engine._advance_trading_day(next_account)

    assert manager.completed_account_returns[0][0] == "20260824"
    assert manager.completed_account_returns[0][1] == pytest.approx(-0.1)
    assert engine.state.trading_day == "20260825"


def test_stress90_completed_return_uses_ctp_settlement_not_last_intraday_snapshot(tmp_path):
    broker, manager, engine = _engine(tmp_path)
    manager.runtime_policy_id = "directional.stress90"
    engine.state.trading_day = "20260824"
    engine.state.day_start_equity = 100_000.0
    engine.state.last_account_trading_day = "20260824"
    engine.state.last_account_equity = 95_000.0
    engine.state.last_account_cash_flow_verified = True
    engine.state.last_account_settlement_id = 10
    next_account = AccountSnapshot(
        76_000.0,
        76_000.0,
        76_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
        previous_settlement_equity=75_000.0,
        settlement_verified=True,
        settlement_id=11,
    )

    engine._advance_trading_day(next_account)

    assert manager.completed_account_returns == [("20260824", -0.25)]
    assert engine.state.day_start_equity == 75_000.0


def test_stress90_partial_inception_day_is_not_added_to_adaptive_margin_returns(tmp_path):
    _broker, manager, engine = _engine(tmp_path)
    manager.runtime_policy_id = "directional.stress90"
    manager.skipped_completed_account_days.add("20260824")
    engine.state.trading_day = "20260824"
    engine.state.day_start_equity = 100_000.0
    engine.state.last_account_trading_day = "20260824"
    engine.state.last_account_equity = 80_000.0
    engine.state.last_account_cash_flow_verified = True
    engine.state.last_account_settlement_id = 10
    engine.state.recent_daily_returns = [0.02]
    next_account = AccountSnapshot(
        80_000.0,
        80_000.0,
        80_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
        previous_settlement_equity=80_000.0,
        settlement_verified=True,
        settlement_id=11,
    )

    engine._advance_trading_day(next_account)

    assert manager.completed_account_returns == []
    assert engine.state.recent_daily_returns == [0.02]
    assert engine.state.trading_day == "20260825"


def test_stress90_new_day_hard_baseline_uses_settlement_not_intraday_equity(tmp_path):
    _broker, manager, engine = _engine(tmp_path)
    manager.runtime_policy_id = "directional.stress90"
    engine.state.trading_day = "20260824"
    engine.state.day_start_equity = 1_000_000.0
    engine.state.last_account_trading_day = "20260824"
    engine.state.last_account_equity = 1_000_000.0
    engine.state.last_account_cash_flow_verified = True
    engine.state.last_account_settlement_id = 10
    intraday_loss_snapshot = AccountSnapshot(
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

    engine._advance_trading_day(intraday_loss_snapshot)

    assert engine.state.day_start_equity == 1_000_000.0


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


@pytest.mark.parametrize(
    ("settlement_id", "previous_settlement_equity"),
    [(11, 1_000_000.0), (10, 1_100_000.0)],
    ids=("settlement-identity-changed", "prebalance-changed"),
)
def test_stress90_same_day_runtime_rejects_settlement_lineage_change(
    tmp_path,
    settlement_id,
    previous_settlement_equity,
):
    _broker, manager, engine = _engine(tmp_path)
    manager.runtime_policy_id = "directional.stress90"
    engine.state.trading_day = "20260825"
    engine.state.day_start_equity = 1_000_000.0
    engine.state.equity_high_watermark = 1_000_000.0
    engine.state.last_account_trading_day = "20260825"
    engine.state.last_account_equity = 1_000_000.0
    engine.state.last_account_cash_flow_verified = True
    engine.state.last_account_settlement_id = 10
    account = AccountSnapshot(
        951_000.0,
        951_000.0,
        951_000.0,
        0.0,
        -49_000.0,
        0.0,
        "20260825",
        previous_settlement_equity=previous_settlement_equity,
        settlement_verified=True,
        settlement_id=settlement_id,
    )

    with pytest.raises(RuntimeError, match="same-day.*settlement"):
        engine._advance_trading_day(account)

    assert engine.state.day_start_equity == 1_000_000.0
    assert engine.state.last_account_settlement_id == 10


def test_stress90_rollover_rejects_unverified_completed_settlement(tmp_path):
    _broker, manager, engine = _engine(tmp_path)
    manager.runtime_policy_id = "directional.stress90"
    engine.state.trading_day = "20260824"
    engine.state.day_start_equity = 100_000.0
    engine.state.last_account_trading_day = "20260824"
    engine.state.last_account_equity = 95_000.0
    engine.state.last_account_cash_flow_verified = True
    engine.state.last_account_settlement_id = 10
    account = AccountSnapshot(95_000, 95_000, 95_000, 0, 0, 0, "20260825")

    with pytest.raises(RuntimeError, match="settlement"):
        engine._advance_trading_day(account)
    assert manager.completed_account_returns == []


def test_stress90_rollover_rejects_unproven_target_day_gap_without_soft_path_advance(
    tmp_path,
):
    _broker, manager, engine = _engine(tmp_path)
    manager.runtime_policy_id = "directional.stress90"
    engine.state.trading_day = "20260824"
    engine.state.day_start_equity = 100_000.0
    engine.state.last_account_trading_day = "20260824"
    engine.state.last_account_equity = 100_000.0
    engine.state.last_account_cash_flow_verified = True
    engine.state.last_account_settlement_id = 10
    account = AccountSnapshot(
        110_000,
        110_000,
        110_000,
        0,
        0,
        0,
        "20260827",
        previous_settlement_equity=110_000,
        settlement_verified=True,
        settlement_id=13,
    )

    with pytest.raises(RuntimeError, match="trading-day gap"):
        engine._advance_trading_day(account)

    assert manager.completed_account_returns == []
    assert engine.state.trading_day == "20260824"
    assert engine.state.day_start_equity == 100_000.0


def test_stress90_unproven_target_day_gap_enters_reduce_only_when_exposed(tmp_path):
    _broker, manager, engine = _engine(tmp_path)
    manager.runtime_policy_id = "directional.stress90"
    manager.risk = True
    engine.state.trading_day = "20260824"
    engine.state.day_start_equity = 100_000.0
    engine.state.last_account_trading_day = "20260824"
    engine.state.last_account_equity = 100_000.0
    engine.state.last_account_cash_flow_verified = True
    engine.state.last_account_settlement_id = 10
    account = AccountSnapshot(
        110_000,
        110_000,
        110_000,
        0,
        0,
        0,
        "20260827",
        previous_settlement_equity=110_000,
        settlement_verified=True,
        settlement_id=13,
    )

    engine._handle_account_event(account)

    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert engine.state.trading_day == "20260824"
    assert manager.completed_account_returns == []


def test_stress90_weekend_rollover_uses_explicit_verified_session_chain(tmp_path):
    _broker, manager, engine = _engine(tmp_path)
    manager.runtime_policy_id = "directional.stress90"
    manager.verified_completed_account_transitions.add(("20260821", "20260824"))
    engine.state.trading_day = "20260821"
    engine.state.day_start_equity = 100_000.0
    engine.state.last_account_trading_day = "20260821"
    engine.state.last_account_equity = 100_000.0
    engine.state.last_account_cash_flow_verified = True
    engine.state.last_account_settlement_id = 10
    account = AccountSnapshot(
        110_000,
        110_000,
        110_000,
        0,
        0,
        0,
        "20260824",
        previous_settlement_equity=110_000,
        settlement_verified=True,
        settlement_id=91,
    )

    engine._advance_trading_day(account)

    assert manager.completed_account_returns == [("20260821", pytest.approx(0.1, abs=1e-15))]
    assert engine.state.trading_day == "20260824"


def test_stress90_account_day_rejects_unverified_or_nonzero_cash_flow(tmp_path):
    broker, manager, engine = _engine(tmp_path)
    manager.runtime_policy_id = "directional.stress90"
    engine.state.trading_day = "20260824"
    engine.state.day_start_equity = 100_000.0
    engine.state.last_account_trading_day = "20260824"
    engine.state.last_account_equity = 110_000.0

    for account in (
        AccountSnapshot(
            110_000.0,
            110_000.0,
            110_000.0,
            0.0,
            0.0,
            0.0,
            "20260825",
            cash_flow_verified=False,
        ),
        AccountSnapshot(
            110_000.0,
            110_000.0,
            110_000.0,
            0.0,
            0.0,
            0.0,
            "20260825",
            deposit=10_000.0,
        ),
    ):
        with pytest.raises(RuntimeError, match="rebase"):
            engine._advance_trading_day(account)
    assert manager.completed_account_returns == []


def test_directional_gross_guard_reduction_keeps_engine_running(tmp_path):
    _, manager, engine = _engine(tmp_path)
    manager.risk = True
    manager.next_gross_guard_result = DirectionalActionResult(
        "reduce", order_ids=("gross-guard-1",)
    )

    engine.on_tick(_tick())
    engine.run_once()

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
    engine.run_once()

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


def test_directional_engine_halts_on_malformed_or_invalid_order_payload(tmp_path):
    _, _, malformed_engine = _engine(tmp_path / "malformed")
    malformed_engine._handle_order_event(object())
    assert malformed_engine.halted
    assert "invalid order event" in malformed_engine.state.kill_reason

    _, _, invalid_engine = _engine(tmp_path / "invalid")
    invalid_engine._handle_order_event(
        Order(
            "owned-order",
            OrderRequest("A2609", "DCE", OrderSide.BUY, Offset.OPEN, 1, 100.0),
            status=OrderStatus.FILLED,
            traded=2,
        )
    )
    assert invalid_engine.halted
    assert "invalid order event" in invalid_engine.state.kill_reason
