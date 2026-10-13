from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from afuture.broker.ctp import CtpBroker, CtpCredentials
from afuture.models import BrokerEvent, Offset, OrderRequest, OrderSide, OrderType

_CHINA = ZoneInfo("Asia/Shanghai")


def _credentials() -> CtpCredentials:
    return CtpCredentials("user", "secret", "9999", "td", "md", "app", "auth", "test")


def _candidate_request(**changes: object) -> OrderRequest:
    values: dict[str, object] = {
        "symbol": "A2701",
        "exchange": "DCE",
        "side": OrderSide.BUY,
        "offset": Offset.OPEN,
        "volume": 2,
        "price": 1_001.0,
        "order_type": OrderType.FAK,
        "reference": "directional:stress90:" + "d" * 12 + ":A",
    }
    values.update(changes)
    return OrderRequest(**values)  # type: ignore[arg-type]


def _configure(broker: CtpBroker, path: Path) -> None:
    broker.configure_order_submission_journal(
        path,
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    broker.set_order_submission_context(
        target_trading_day="20260825",
        daily_decision_digest="d" * 64,
        execution_intent_digest="e" * 64,
        transition={
            "freeze_authorized_lots": {"A2701": 2},
            "transitions": [
                {
                    "product": "A",
                    "kind": "same_product_roll",
                    "source_symbols": ["A2612"],
                    "target_symbol": "A2701",
                    "target_sign": 1,
                    "max_replacement_notional": 50_000.0,
                }
            ],
        },
    )


def _attach_gateway(
    broker: CtpBroker,
    *,
    before_official_send=None,
) -> tuple[SimpleNamespace, list[OrderRequest]]:
    td_api = SimpleNamespace(
        order_ref=40,
        frontid=7,
        sessionid=11,
        getTradingDay=lambda: "20260825",
    )
    gateway = SimpleNamespace(td_api=td_api)
    calls: list[OrderRequest] = []

    def send_order(request, gateway_name):
        assert gateway_name == "CTP"
        if before_official_send is not None:
            before_official_send()
        td_api.order_ref += 1
        calls.append(request)
        return f"CTP.{td_api.frontid}_{td_api.sessionid}_{td_api.order_ref}"

    broker._main_engine = SimpleNamespace(
        get_gateway=lambda _name: gateway,
        send_order=send_order,
    )
    broker.is_ready = lambda: True
    broker._to_vnpy_order = lambda request: request
    return td_api, calls


def _authorize(broker: CtpBroker, *requests: OrderRequest) -> None:
    broker.authorize_candidate_order_requests(requests)


def _raw_trade(order_id: str, trade_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        vt_tradeid=f"CTP.{trade_id}",
        vt_orderid=order_id,
        symbol="A2701",
        exchange=SimpleNamespace(value="DCE"),
        direction=SimpleNamespace(name="LONG"),
        offset=SimpleNamespace(name="OPEN"),
        volume=1,
        price=1_000.0,
        datetime=datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
        _afuture_trading_day="20260825",
    )


@pytest.mark.parametrize("during_prepare", ["none", "quote", "delay", "at_send"])
def test_manager_opening_rechecks_after_journal_before_send(tmp_path, monkeypatch, during_prepare):
    from dataclasses import replace
    from datetime import timedelta

    from test_directional_stress90_runtime import _mechanical_manager

    from afuture.models import FeeSpec

    manager, account_broker, _, now = _mechanical_manager(tmp_path, target_weight=1.0)
    broker = CtpBroker(_credentials())
    _configure(broker, tmp_path / "ctp.json")
    broker.set_order_submission_context(
        target_trading_day="20260825", daily_decision_digest="d" * 64,
        execution_intent_digest="e" * 64,
        transition={"freeze_authorized_lots": {"A2612": 1}, "transitions": []},
    )
    _, calls = _attach_gateway(broker)
    broker.get_account = account_broker.get_account
    broker.get_positions = account_broker.get_positions
    broker.get_active_orders = account_broker.get_active_orders
    broker.health_error = lambda: None
    broker.metadata_query_blocks = False
    manager.broker = broker
    manager.historical_mode = False
    wall = [now]
    manager.health_clock = lambda: wall[0]
    tick = replace(manager._ticks["A2612"], bid_price=9999, ask_price=10001, last_price=10000)
    manager._ticks["A2612"] = tick
    manager._specs["A2612"] = replace(manager._specs["A2612"], fee=FeeSpec(open_fixed=140))
    if during_prepare == "at_send":
        _, calls = _attach_gateway(
            broker, before_official_send=lambda: broker._enqueue_tick(replace(tick, bid_price=9997))
        )
    prepare = broker._order_submission_journal.prepare

    def delayed_prepare(*args, **kwargs):
        record = prepare(*args, **kwargs)
        if during_prepare == "quote":
            broker._enqueue_tick(replace(tick, bid_price=9997))
        elif during_prepare == "delay":
            wall[0] += timedelta(seconds=30)
        return record

    monkeypatch.setattr(broker._order_submission_journal, "prepare", delayed_prepare)
    result = manager._submit_openings(
        [], {"A2612": 1}, {"A": manager._catalog_by_symbol["A2612"]}, manager._specs, now,
        reference_prefix="directional:stress90:" + "d" * 12,
    )
    entry = broker._order_submission_journal.get_entry("CTP.7_11_41")
    assert entry is not None
    if during_prepare == "none":
        assert result.action == "open" and len(calls) == 1
        assert entry.status == "submitted"
    else:
        assert result.action == "reject" and calls == []
        assert entry.status == "aborted_before_send"


def test_candidate_authorization_binds_and_consumes_one_exact_request(tmp_path: Path) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _td_api, calls = _attach_gateway(broker)
    authorized = _candidate_request()
    _authorize(broker, authorized)

    for mismatch in (
        _candidate_request(symbol="M2701"),
        _candidate_request(exchange="SHFE"),
        _candidate_request(side=OrderSide.SELL),
        _candidate_request(volume=35),
        _candidate_request(price=1_002.0),
        _candidate_request(order_type=OrderType.LIMIT),
        _candidate_request(reference="manual:unplanned"),
    ):
        with pytest.raises(RuntimeError, match="exact candidate request"):
            broker.send_order(mismatch)
    assert calls == []

    assert broker.send_order(authorized) == "CTP.7_11_41"
    with pytest.raises(RuntimeError, match="exact candidate request"):
        broker.send_order(authorized)
    assert calls == [authorized]


def test_fatal_callback_at_official_boundary_interrupts_before_external_send(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _authorize(broker, _candidate_request())

    def fatal_before_increment() -> None:
        with broker._critical_callback_scope():
            broker._enqueue_critical(BrokerEvent("broker_error", "fatal at boundary"))

    _td_api, calls = _attach_gateway(broker, before_official_send=fatal_before_increment)

    with pytest.raises(RuntimeError, match="critical event arrived during CTP submission"):
        broker.send_order(_candidate_request())
    assert calls == []


def test_fill_arriving_during_checkpoint_is_subtracted_not_replayed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _authorize(broker, _candidate_request())
    _attach_gateway(broker)
    order_id = broker.send_order(_candidate_request())
    broker._on_trade(SimpleNamespace(data=_raw_trade(order_id, "T1")))
    assert [event.event_type for event in broker.poll_events()] == ["trade"]

    entered = threading.Event()
    release = threading.Event()
    original = broker._order_submission_journal.apply_updates

    def blocked_apply(*, statuses=None, fills=None):
        entered.set()
        assert release.wait(2.0)
        return original(statuses=statuses, fills=fills)

    monkeypatch.setattr(broker._order_submission_journal, "apply_updates", blocked_apply)
    worker = threading.Thread(target=broker.checkpoint_order_submission_journal)
    worker.start()
    try:
        assert entered.wait(1.0)
        broker._on_trade(SimpleNamespace(data=_raw_trade(order_id, "T2")))
    finally:
        release.set()
        worker.join(2.0)
    assert not worker.is_alive()
    assert [event.event_type for event in broker.poll_events()] == ["trade"]

    broker.checkpoint_order_submission_journal()

    entry = CtpOrderSubmissionJournal(path).get_entry(order_id)
    assert entry is not None
    assert entry.filled_volume == 2
    assert entry.fill_keys == ("20260825:DCE:CTP.T1", "20260825:DCE:CTP.T2")
    assert broker._pending_order_journal_fills == {}


def test_source_day_survives_queued_fill_and_old_day_duplicate(tmp_path):
    from afuture.engine import TradingEngine
    from afuture.models import AccountSnapshot, ContractPosition
    from afuture.risk import RiskConfig, RiskManager
    from afuture.state import StateStore

    broker = CtpBroker(_credentials())
    td, _ = _attach_gateway(broker)
    broker._order_references["CTP.O1"] = "directional:A"
    account = AccountSnapshot(100000, 100000, 100000, 0, 0, 0, "20260825")
    broker.get_account = lambda: account
    engine = TradingEngine(broker, [], {}, RiskManager(RiskConfig()), StateStore(tmp_path / "s.json"))
    engine.state.trading_day = "20260825"
    raw = _raw_trade("CTP.O1", "T1")
    broker._on_trade(SimpleNamespace(data=raw))
    event, = broker.poll_events()
    assert event.payload.trading_day == "20260825"
    td.getTradingDay = lambda: "20260826"
    account = AccountSnapshot(100000, 100000, 100000, 0, 0, 0, "20260826")
    assert engine._handle_trade_event(event.payload)
    assert engine.state.recent_trade_ids == ["20260825:DCE:CTP.T1"]
    engine._advance_trading_day(account)
    engine._reconcile_runtime_snapshot([ContractPosition("A2701", "DCE", long_yesterday=1, long_price=1000)])
    assert not engine.halted
    assert not engine._handle_trade_event(event.payload)
    broker._on_trade(SimpleNamespace(data=raw))
    assert broker.poll_events() == []
    assert broker.get_positions()[0].long_total == 1
    del raw._afuture_trading_day
    broker._on_trade(SimpleNamespace(data=raw))
    assert broker.poll_events()[0].event_type == "broker_error"
