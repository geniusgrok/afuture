from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from afuture.alerts import AlertManager, MemoryAlertSink
from afuture.broker.sim import SimBroker
from afuture.engine import TradingEngine
from afuture.execution import PairExecutor
from afuture.models import (
    AccountSnapshot, BrokerEvent, ContractPosition, ContractSpec, Offset, Order,
    OrderRequest, OrderSide, OrderStatus, PairConfig, RuntimeMode, SignalAction,
    SpreadSignal, Tick, Trade,
)
from afuture.position import PositionBook
from afuture.risk import RiskConfig, RiskManager
from afuture.state import RuntimeState, StateStore


def tick(symbol, bid, ask, minute=0, *, depth=20, ts=None):
    return Tick(symbol, "DCE", ts or datetime(2026, 8, 21, 9, minute, tzinfo=timezone.utc),
                bid, ask, (bid+ask)/2, depth, depth, "20260821")


def setup_specs():
    return {s: ContractSpec(s, "DCE", 10, 1, .1, .1) for s in ("m2609", "m2701")}


def test_executor_blocks_negative_net_edge_then_submits_profitable_dynamic_size():
    specs = setup_specs(); broker = SimBroker(500000, specs); broker.start()
    near, far = tick("m2609", 3000, 3001), tick("m2701", 2999, 3000)
    broker.publish_tick(near); broker.publish_tick(far)
    executor = PairExecutor(broker, RiskManager(RiskConfig(risk_budget_ratio=.01)), specs)
    pair = PairConfig("p", "m2609", "m2701", "DCE", 5, min_net_edge=1e9)
    signal = SpreadSignal("p", SignalAction.SHORT_SPREAD, 3, near.timestamp, 1, 0, 10)
    assert not executor.execute_signal(pair, signal, near, far, open_pair_count=0, spread_std=10).accepted
    profitable = PairConfig("p", "m2609", "m2701", "DCE", 5, min_net_edge=0)
    near2 = tick("m2609", 3030, 3031); broker.publish_tick(near2)
    signal2 = SpreadSignal("p", SignalAction.SHORT_SPREAD, 3, near2.timestamp, 31, 10, 5)
    result = executor.execute_signal(profitable, signal2, near2, far, open_pair_count=0, spread_std=5)
    assert result.accepted and 1 <= result.volume <= 5


def test_second_leg_failure_rolls_back_filled_first_leg():
    specs = setup_specs()
    class RejectSecond(SimBroker):
        def __init__(self): super().__init__(500000, specs); self.calls = 0
        def send_order(self, request):
            self.calls += 1
            if self.calls == 2: raise RuntimeError("second leg rejected")
            return super().send_order(request)
    broker = RejectSecond(); broker.start()
    near, far = tick("m2609", 3020, 3021), tick("m2701", 2999, 3000)
    broker.publish_tick(near); broker.publish_tick(far)
    executor = PairExecutor(broker, RiskManager(RiskConfig()), specs)
    pair = PairConfig("p", "m2609", "m2701", "DCE", 1)
    signal = SpreadSignal("p", SignalAction.SHORT_SPREAD, 3, near.timestamp, 20, 10, 5)
    result = executor.execute_signal(pair, signal, near, far, open_pair_count=0, spread_std=5)
    assert not result.accepted and broker.get_positions() == [] and broker.calls == 3


def test_engine_enters_reduce_only_then_halts_after_risk_removed(tmp_path: Path):
    specs = setup_specs(); pair = PairConfig("p", "m2609", "m2701", "DCE", 2)
    broker = SimBroker(500000, specs); broker.position_book = PositionBook([ContractPosition("m2609", "DCE", long_today=1)])
    store = StateStore(tmp_path / "state.json"); store.save(RuntimeState(positions=[asdict(ContractPosition("m2609","DCE",long_today=1))]))
    engine = TradingEngine(broker, [pair], specs, RiskManager(RiskConfig()), store, legging_timeout_seconds=0)
    engine.start(); broker.publish_tick(tick("m2609",3000,3001)); broker.publish_tick(tick("m2701",2990,2991)); engine.run_once()
    assert engine.state.runtime_mode in {RuntimeMode.REDUCE_ONLY.value, RuntimeMode.HALTED.value}
    broker.position_book = PositionBook(); engine.state.positions = []; engine.run_once()
    assert engine.state.runtime_mode == RuntimeMode.HALTED.value and engine.state.kill_switch


def test_auto_flatten_false_does_not_send_repair_orders(tmp_path: Path):
    specs = setup_specs(); pair = PairConfig("p", "m2609", "m2701", "DCE", 2)
    broker = SimBroker(500000, specs); broker.position_book = PositionBook([ContractPosition("m2609", "DCE", long_today=1)])
    store = StateStore(tmp_path / "state.json"); store.save(RuntimeState(positions=[asdict(ContractPosition("m2609","DCE",long_today=1))]))
    engine = TradingEngine(broker, [pair], specs, RiskManager(RiskConfig()), store,
                           legging_timeout_seconds=0, auto_flatten_imbalance=False, historical_mode=True)
    engine.start(); broker.publish_tick(tick("m2609",3000,3001)); broker.publish_tick(tick("m2701",2990,2991)); engine.run_once()
    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert broker.get_orders() == []


def test_live_wall_clock_detects_total_market_freeze(tmp_path: Path):
    specs = setup_specs(); pair = PairConfig("p", "m2609", "m2701", "DCE", 1)
    broker = SimBroker(500000, specs); now = datetime(2026,8,21,9,0,tzinfo=timezone.utc)
    engine = TradingEngine(broker, [pair], specs, RiskManager(RiskConfig(max_quote_age_seconds=5)),
                           StateStore(tmp_path/"s.json"), health_clock=lambda: now + timedelta(seconds=10))
    engine.start(); broker.publish_tick(tick("m2609",3000,3001,ts=now)); broker.publish_tick(tick("m2701",2990,2991,ts=now)); engine.run_once()
    assert engine.halted and "stale" in engine.state.kill_reason


def test_historical_health_uses_event_time_not_wall_clock(tmp_path: Path):
    specs = setup_specs(); pair = PairConfig("p", "m2609", "m2701", "DCE", 1)
    old = datetime(2022,1,1,9,0,tzinfo=timezone.utc); broker = SimBroker(500000, specs)
    engine = TradingEngine(broker, [pair], specs, RiskManager(RiskConfig(max_quote_age_seconds=5)),
                           StateStore(tmp_path/"s.json"), historical_mode=True)
    engine.start(); broker.publish_tick(tick("m2609",3000,3001,ts=old)); broker.publish_tick(tick("m2701",2990,2991,ts=old)); engine.run_once()
    assert not engine.halted


def test_metadata_failure_cannot_be_cleared_by_position_reconcile(tmp_path: Path):
    specs = setup_specs()
    class BadMetadataBroker(SimBroker):
        def get_live_contract_specs(self, symbols, timeout_seconds=10):
            bad = dict(specs); bad["m2609"] = ContractSpec("m2609", "DCE", 10, 1, .2, .2); return bad
    broker = BadMetadataBroker(500000, specs); store = StateStore(tmp_path/"s.json")
    store.save(RuntimeState(kill_switch=True, positions=[]))
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store, require_live_metadata=True)
    engine.start()
    assert engine.halted and not engine.state.metadata_verified
    assert not engine.clear_kill_switch_after_reconcile()


def test_unknown_trade_halts_without_adopting_position_and_persists_ids(tmp_path: Path):
    specs = setup_specs()
    class ExternalBroker(SimBroker):
        def owns_order(self, order_id): return False
    broker = ExternalBroker(500000, specs); store = StateStore(tmp_path/"s.json")
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store); engine.start()
    trade = Trade("external","manual","m2609","DCE",OrderSide.BUY,Offset.OPEN,1,3000,datetime.now(timezone.utc))
    broker._events.append(BrokerEvent("trade", trade)); engine.run_once()
    assert engine.halted and store.positions_from_state(store.load()) == []


def test_known_order_and_trade_ids_are_persisted(tmp_path: Path):
    specs = setup_specs(); broker = SimBroker(500000, specs); store = StateStore(tmp_path/"s.json")
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), store); engine.start()
    request = OrderRequest("m2609","DCE",OrderSide.BUY,Offset.OPEN,1,3001)
    broker.publish_tick(tick("m2609",3000,3001)); oid = broker.send_order(request); engine.run_once()
    state = store.load()
    assert state.last_order_id == oid and state.last_trade_id.startswith("SIM-T-")


def test_critical_alert_is_emitted_on_halt(tmp_path: Path):
    sink = MemoryAlertSink(); specs = setup_specs(); broker = SimBroker(500000, specs)
    engine = TradingEngine(broker, [], specs, RiskManager(RiskConfig()), StateStore(tmp_path/"s.json"),
                           alert_manager=AlertManager([sink]))
    engine.start(); engine.emergency_stop("test")
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
