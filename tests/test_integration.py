from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from afuture.alerts import AlertManager, MemoryAlertSink
from afuture.broker.ctp import CtpBroker, CtpCredentials
from afuture.broker.sim import SimBroker
from afuture.engine import TradingEngine
from afuture.execution import PairExecutor
from afuture.models import (
    AccountSnapshot,
    BrokerEvent,
    ContractPosition,
    ContractSpec,
    Offset,
    OrderRequest,
    OrderSide,
    PairConfig,
    RuntimeMode,
    SignalAction,
    SpreadSignal,
    Tick,
    Trade,
)
from afuture.position import PositionBook
from afuture.risk import RiskConfig, RiskManager
from afuture.state import RuntimeState, StateStore


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


def test_executor_blocks_negative_net_edge_then_submits_profitable_dynamic_size():
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    broker.start()
    near, far = tick("m2609", 3000, 3001), tick("m2701", 2999, 3000)
    broker.publish_tick(near)
    broker.publish_tick(far)
    executor = PairExecutor(
        broker, RiskManager(RiskConfig(risk_budget_ratio=0.01)), specs, historical_mode=True
    )
    pair = PairConfig("p", "m2609", "m2701", "DCE", 5, min_net_edge=1e9)
    signal = SpreadSignal("p", SignalAction.SHORT_SPREAD, 3, near.timestamp, 1, 0, 10)
    assert not executor.execute_signal(
        pair, signal, near, far, open_pair_count=0, spread_std=10
    ).accepted
    profitable = PairConfig("p", "m2609", "m2701", "DCE", 5, min_net_edge=0)
    near2 = tick("m2609", 3030, 3031)
    broker.publish_tick(near2)
    signal2 = SpreadSignal("p", SignalAction.SHORT_SPREAD, 3, near2.timestamp, 31, 10, 5)
    result = executor.execute_signal(
        profitable, signal2, near2, far, open_pair_count=0, spread_std=5
    )
    assert result.accepted and 1 <= result.volume <= 5


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


def test_engine_enters_reduce_only_then_halts_after_risk_removed(tmp_path: Path):
    specs = setup_specs()
    pair = PairConfig("p", "m2609", "m2701", "DCE", 2)
    broker = SimBroker(500000, specs)
    broker.position_book = PositionBook(
        [ContractPosition("m2609", "DCE", long_today=1, long_price=3000)]
    )
    store = StateStore(tmp_path / "state.json")
    store.save(
        RuntimeState(
            positions=[asdict(ContractPosition("m2609", "DCE", long_today=1, long_price=3000))]
        )
    )
    engine = TradingEngine(
        broker, [pair], specs, RiskManager(RiskConfig()), store, legging_timeout_seconds=0
    )
    engine.start()
    broker.publish_tick(tick("m2609", 3000, 3001))
    broker.publish_tick(tick("m2701", 2990, 2991))
    engine.run_once()
    assert engine.state.runtime_mode in {RuntimeMode.REDUCE_ONLY.value, RuntimeMode.HALTED.value}
    broker.position_book = PositionBook()
    engine.state.positions = []
    engine.run_once()
    assert engine.state.runtime_mode == RuntimeMode.HALTED.value and engine.state.kill_switch


def test_auto_flatten_false_does_not_send_repair_orders(tmp_path: Path):
    specs = setup_specs()
    pair = PairConfig("p", "m2609", "m2701", "DCE", 2)
    broker = SimBroker(500000, specs)
    broker.position_book = PositionBook(
        [ContractPosition("m2609", "DCE", long_today=1, long_price=3000)]
    )
    store = StateStore(tmp_path / "state.json")
    store.save(
        RuntimeState(
            positions=[asdict(ContractPosition("m2609", "DCE", long_today=1, long_price=3000))]
        )
    )
    engine = TradingEngine(
        broker,
        [pair],
        specs,
        RiskManager(RiskConfig()),
        store,
        legging_timeout_seconds=0,
        auto_flatten_imbalance=False,
        historical_mode=True,
    )
    engine.start()
    broker.publish_tick(tick("m2609", 3000, 3001))
    broker.publish_tick(tick("m2701", 2990, 2991))
    engine.run_once()
    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert broker.get_orders() == []


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


def test_historical_health_uses_event_time_not_wall_clock(tmp_path: Path):
    specs = setup_specs()
    pair = PairConfig("p", "m2609", "m2701", "DCE", 1)
    old = datetime(2022, 1, 1, 9, 0, tzinfo=timezone.utc)
    broker = SimBroker(500000, specs)
    engine = TradingEngine(
        broker,
        [pair],
        specs,
        RiskManager(RiskConfig(max_quote_age_seconds=5)),
        StateStore(tmp_path / "s.json"),
        historical_mode=True,
    )
    engine.start()
    broker.publish_tick(tick("m2609", 3000, 3001, ts=old))
    broker.publish_tick(tick("m2701", 2990, 2991, ts=old))
    engine.run_once()
    assert not engine.halted


def test_metadata_failure_cannot_be_cleared_by_position_reconcile(tmp_path: Path):
    specs = setup_specs()

    class BadMetadataBroker(SimBroker):
        def get_live_contract_specs(self, symbols, timeout_seconds=10):
            bad = dict(specs)
            bad["m2609"] = ContractSpec("m2609", "DCE", 10, 1, 0.2, 0.2)
            return bad

    broker = BadMetadataBroker(500000, specs)
    store = StateStore(tmp_path / "s.json")
    store.save(RuntimeState(kill_switch=True, positions=[]))
    engine = TradingEngine(
        broker, [], specs, RiskManager(RiskConfig()), store, require_live_metadata=True
    )
    engine.start()
    assert engine.halted and not engine.state.metadata_verified
    assert not engine.clear_kill_switch_after_reconcile()


def test_engine_start_fails_closed_on_invalid_account_snapshot(tmp_path: Path):
    specs = setup_specs()

    class InvalidAccountBroker(SimBroker):
        def get_account(self):
            return AccountSnapshot(
                500000,
                float("nan"),
                400000,
                100000,
                0,
                0,
                "20260821",
            )

    broker = InvalidAccountBroker(500000, specs)
    engine = TradingEngine(
        broker,
        [],
        specs,
        RiskManager(RiskConfig()),
        StateStore(tmp_path / "s.json"),
    )

    engine.start()

    assert engine.halted
    assert "invalid account snapshot" in engine.state.kill_reason


def test_account_event_is_validated_before_runtime_state_mutation(tmp_path: Path):
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    store = StateStore(tmp_path / "s.json")
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()
    before_day = engine.state.trading_day
    broker._events.append(
        BrokerEvent(
            "account",
            AccountSnapshot(500000, float("nan"), 400000, 100000, 0, 0, "20990101"),
        )
    )

    engine.run_once()

    assert engine.halted
    assert engine.state.trading_day == before_day
    assert "invalid account snapshot" in engine.state.kill_reason


def test_delayed_prior_day_account_event_halts_without_rebucketing_positions(tmp_path: Path):
    specs = setup_specs()
    persisted_position = ContractPosition(
        "m2609",
        "DCE",
        long_today=1,
        long_price=3000.0,
    )
    broker = SimBroker(500000, specs)
    broker._trading_day = "20260825"
    broker.position_book = PositionBook([persisted_position])
    store = StateStore(tmp_path / "s.json")
    store.save(
        RuntimeState(
            trading_day="20260825",
            positions=[asdict(persisted_position)],
        )
    )
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()

    engine._handle_account_event(AccountSnapshot(500000, 500000, 500000, 0, 0, 0, "20260824"))

    saved = store.load()
    position = store.positions_from_state(saved)[0]
    assert engine.halted
    assert "moved backward" in engine.state.kill_reason
    assert saved.trading_day == "20260825"
    assert (position.long_today, position.long_yesterday) == (1, 0)


def test_restart_with_persisted_day_ahead_of_broker_halts_without_rebucketing(
    tmp_path: Path,
):
    specs = setup_specs()
    persisted_position = ContractPosition(
        "m2609",
        "DCE",
        long_today=1,
        long_price=3000.0,
    )
    broker = SimBroker(500000, specs)
    broker._trading_day = "20260824"
    broker.position_book = PositionBook([persisted_position])
    store = StateStore(tmp_path / "s.json")
    store.save(
        RuntimeState(
            trading_day="20260825",
            positions=[asdict(persisted_position)],
        )
    )
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)

    engine.start()

    saved = store.load()
    position = store.positions_from_state(saved)[0]
    assert engine.halted
    assert "moved backward" in engine.state.kill_reason
    assert saved.trading_day == "20260825"
    assert (position.long_today, position.long_yesterday) == (1, 0)


def test_prior_and_current_day_account_backlog_halts_without_bucket_bounce(tmp_path: Path):
    specs = setup_specs()
    persisted_position = ContractPosition(
        "m2609",
        "DCE",
        long_today=1,
        long_price=3000.0,
    )
    broker = SimBroker(500000, specs)
    broker._trading_day = "20260825"
    broker.position_book = PositionBook([persisted_position])
    store = StateStore(tmp_path / "s.json")
    store.save(
        RuntimeState(
            trading_day="20260825",
            positions=[asdict(persisted_position)],
        )
    )
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()
    broker._events.extend(
        [
            BrokerEvent(
                "account",
                AccountSnapshot(500000, 500000, 500000, 0, 0, 0, "20260824"),
            ),
            BrokerEvent(
                "account",
                AccountSnapshot(500000, 500000, 500000, 0, 0, 0, "20260825"),
            ),
        ]
    )

    engine.run_once()

    saved = store.load()
    position = store.positions_from_state(saved)[0]
    assert engine.halted
    assert "moved backward" in engine.state.kill_reason
    assert saved.trading_day == "20260825"
    assert (position.long_today, position.long_yesterday) == (1, 0)


def test_unknown_trade_halts_without_adopting_position_and_persists_ids(tmp_path: Path):
    specs = setup_specs()

    class ExternalBroker(SimBroker):
        def owns_order(self, order_id):
            return False

    broker = ExternalBroker(500000, specs)
    store = StateStore(tmp_path / "s.json")
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()
    trade = Trade(
        "external",
        "manual",
        "m2609",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        3000,
        datetime.now(timezone.utc),
    )
    broker._events.append(BrokerEvent("trade", trade))
    engine.run_once()
    assert engine.halted and store.positions_from_state(store.load()) == []


def test_known_order_and_trade_ids_are_persisted(tmp_path: Path):
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    broker._trading_day = "20260821"
    store = StateStore(tmp_path / "s.json")
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()
    request = OrderRequest("m2609", "DCE", OrderSide.BUY, Offset.OPEN, 1, 3001)
    broker.publish_tick(tick("m2609", 3000, 3001))
    oid = broker.send_order(request)
    engine.run_once()
    state = store.load()
    assert state.last_order_id == oid and state.last_trade_id.startswith("SIM-T-")


def test_duplicate_trade_callback_is_ignored_before_position_side_effects(tmp_path: Path):
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    broker._trading_day = "20260821"
    store = StateStore(tmp_path / "s.json")
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()
    broker.publish_tick(tick("m2609", 3000, 3001))
    broker.send_order(OrderRequest("m2609", "DCE", OrderSide.BUY, Offset.OPEN, 1, 3001))
    engine.run_once()
    trade = broker.get_trades()[0]
    before = store.load().positions

    broker._events.append(BrokerEvent("trade", trade))
    engine.run_once()

    state = store.load()
    assert state.positions == before
    assert state.recent_trade_ids == [f"20260821:DCE:{trade.trade_id}"]


def test_duplicate_trade_callback_is_ignored_after_restart(tmp_path: Path):
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    broker._trading_day = "20260821"
    store = StateStore(tmp_path / "s.json")
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()
    broker.publish_tick(tick("m2609", 3000, 3001))
    broker.send_order(OrderRequest("m2609", "DCE", OrderSide.BUY, Offset.OPEN, 1, 3001))
    engine.run_once()
    trade = broker.get_trades()[0]
    before = store.load().positions

    restarted = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    restarted.start()
    broker.owns_order = lambda _order_id: False
    broker._events.append(BrokerEvent("trade", trade))
    restarted.run_once()

    state = store.load()
    assert state.positions == before
    assert state.recent_trade_ids == [f"20260821:DCE:{trade.trade_id}"]
    assert not restarted.halted


def test_unqualified_trade_identity_halts_for_reconciliation(tmp_path: Path):
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    broker._trading_day = "20260821"
    store = StateStore(tmp_path / "s.json")
    store.save(
        RuntimeState(
            trading_day="20260821",
            recent_trade_ids=["20260821:UNQUALIFIED-T1"],
        )
    )
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()
    broker.owns_order = lambda _order_id: False
    replay = Trade(
        "UNQUALIFIED-T1",
        "KNOWN-O1",
        "m2609",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        3001.0,
        datetime(2026, 8, 21, 1, tzinfo=timezone.utc),
    )

    handled = engine._handle_trade_event(replay)

    assert not handled
    assert engine.halted
    assert "ambiguous unqualified trade identity" in engine.state.kill_reason
    assert store.positions_from_state(store.load()) == []
    assert store.load().recent_trade_ids == ["20260821:UNQUALIFIED-T1"]


def test_ambiguous_unqualified_trade_identity_halts_on_owned_cross_exchange_fill(
    tmp_path: Path,
):
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    broker._trading_day = "20260821"
    store = StateStore(tmp_path / "s.json")
    persisted_dce_position = ContractPosition(
        "same",
        "DCE",
        long_today=1,
        long_price=100.0,
    )
    store.save(
        RuntimeState(
            trading_day="20260821",
            positions=[asdict(persisted_dce_position)],
            recent_trade_ids=["20260821:COLLIDE"],
        )
    )
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()
    broker.owns_order = lambda _order_id: True
    shfe_fill = Trade(
        "COLLIDE",
        "OWNED-SHFE-O1",
        "same",
        "SHFE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        200.0,
        datetime(2026, 8, 21, 1, tzinfo=timezone.utc),
    )

    handled = engine._handle_trade_event(shfe_fill)

    assert not handled
    assert engine.halted
    assert "ambiguous unqualified trade identity" in engine.state.kill_reason
    positions = store.positions_from_state(store.load())
    assert [(position.symbol, position.exchange) for position in positions] == [("same", "DCE")]


def test_engine_seeds_fresh_ctp_before_startup_replay_mutates_position_mirror(
    tmp_path: Path,
):
    trading_day = "20260825"
    persisted_position = ContractPosition(
        "cu2609",
        "SHFE",
        long_today=1,
        long_price=70_000.0,
    )
    store = StateStore(tmp_path / "s.json")
    store.save(
        RuntimeState(
            trading_day=trading_day,
            day_start_equity=500_000,
            positions=[asdict(persisted_position)],
            recent_trade_ids=[f"{trading_day}:SHFE:CTP.T1"],
        )
    )
    broker = CtpBroker(CtpCredentials("user", "secret", "9999", "td", "md", "app", "auth", "test"))
    broker._last_account = AccountSnapshot(
        500_000,
        500_000,
        500_000,
        0,
        0,
        0,
        trading_day,
    )

    def start_with_inclusive_snapshot_and_replay() -> None:
        broker._trading_day = trading_day
        broker._positions = {
            (persisted_position.symbol, persisted_position.exchange): persisted_position
        }
        broker._on_trade(
            SimpleNamespace(
                data=SimpleNamespace(
                    vt_tradeid="CTP.T1",
                    vt_orderid="CTP.O1",
                    symbol="cu2609",
                    exchange=SimpleNamespace(value="SHFE"),
                    direction=SimpleNamespace(name="LONG"),
                    offset=SimpleNamespace(name="OPEN"),
                    volume=1,
                    price=70_000.0,
                    datetime=datetime(2026, 8, 25, 1, tzinfo=timezone.utc),
                )
            )
        )

    broker.start = start_with_inclusive_snapshot_and_replay
    broker.is_ready = lambda: True
    broker.health_error = lambda: None
    engine = TradingEngine(broker, [], {}, RiskManager(RiskConfig()), store)

    engine.start()
    engine.run_once()

    assert broker.get_positions()[0].long_today == 1
    assert store.positions_from_state(store.load())[0].long_today == 1
    assert broker.delivery_counters()["critical_enqueued"] == 0
    assert not engine.halted


def test_trade_identity_and_expected_positions_include_exchange(tmp_path: Path):
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    broker._trading_day = "20260821"
    store = StateStore(tmp_path / "s.json")
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()
    broker.owns_order = lambda _order_id: True
    for exchange, order_id in (("DCE", "O-DCE"), ("SHFE", "O-SHFE")):
        trade = Trade(
            "SHARED-T1",
            order_id,
            "same",
            exchange,
            OrderSide.BUY,
            Offset.OPEN,
            1,
            100.0,
            datetime(2026, 8, 21, 1, tzinfo=timezone.utc),
        )
        assert engine._handle_trade_event(trade)

    state = store.load()
    positions = sorted(store.positions_from_state(state), key=lambda position: position.exchange)
    assert state.recent_trade_ids == [
        "20260821:DCE:SHARED-T1",
        "20260821:SHFE:SHARED-T1",
    ]
    assert [
        (position.symbol, position.exchange, position.long_today) for position in positions
    ] == [
        ("same", "DCE", 1),
        ("same", "SHFE", 1),
    ]


def test_new_day_trade_rolls_state_before_fill_and_remains_idempotent(tmp_path: Path):
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    broker._trading_day = "20260821"
    store = StateStore(tmp_path / "s.json")
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    engine.start()

    broker._trading_day = "20260822"
    broker.owns_order = lambda _order_id: True
    trade = Trade(
        "NEW-DAY-T1",
        "KNOWN-O1",
        "m2609",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        3001.0,
        datetime(2026, 8, 22, 1, tzinfo=timezone.utc),
    )
    broker._events.append(BrokerEvent("trade", trade))
    engine.run_once()

    state = store.load()
    assert state.trading_day == "20260822"
    assert state.positions[0]["long_today"] == 1
    assert state.positions[0]["long_yesterday"] == 0
    assert state.recent_trade_ids == ["20260822:DCE:NEW-DAY-T1"]

    broker._events.extend(
        [
            BrokerEvent("trade", trade),
            BrokerEvent("account", broker.get_account()),
            BrokerEvent("trade", trade),
        ]
    )
    engine.run_once()
    assert store.load().positions == state.positions

    restarted = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store)
    restarted.start()
    broker.owns_order = lambda _order_id: False
    broker._events.append(BrokerEvent("trade", trade))
    restarted.run_once()
    final_state = store.load()
    assert final_state.positions == state.positions
    assert final_state.recent_trade_ids == ["20260822:DCE:NEW-DAY-T1"]
    assert not restarted.halted


def test_critical_alert_is_emitted_on_halt(tmp_path: Path):
    sink = MemoryAlertSink()
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    engine = TradingEngine(
        broker,
        [],
        specs,
        RiskManager(RiskConfig()),
        StateStore(tmp_path / "s.json"),
        alert_manager=AlertManager([sink]),
    )
    engine.start()
    engine.emergency_stop("test")
    assert sink.events and sink.events[-1]["level"] == "CRITICAL"


def test_alert_sink_failure_is_observable_without_blocking_other_sinks(caplog):
    class FailingSink:
        def send(self, event):
            raise RuntimeError("webhook unavailable")

    receiving_sink = MemoryAlertSink()
    manager = AlertManager([FailingSink(), receiving_sink])

    manager.critical("risk halt")

    assert receiving_sink.events[-1]["message"] == "risk halt"
    assert "FailingSink" in caplog.text
    assert "RuntimeError" in caplog.text


def test_engine_stop_closes_alert_workers(tmp_path: Path):
    class CloseTrackingAlerts(AlertManager):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        def close(self, *, timeout_seconds: float = 1.0) -> None:
            del timeout_seconds
            self.closed = True

    alerts = CloseTrackingAlerts()
    specs = setup_specs()
    broker = SimBroker(500000, specs)
    engine = TradingEngine(
        broker,
        [],
        specs,
        RiskManager(RiskConfig()),
        StateStore(tmp_path / "s.json"),
        alert_manager=alerts,
    )
    engine.start()

    engine.stop()

    assert alerts.closed is True
