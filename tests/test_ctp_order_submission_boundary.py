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


def _reduction_request() -> OrderRequest:
    return OrderRequest(
        "A2701",
        "DCE",
        OrderSide.SELL,
        Offset.CLOSE,
        2,
        999.0,
        OrderType.FAK,
        "directional:gross-guard",
    )


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
    )


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


def test_candidate_authorization_rejects_aggregate_open_above_persisted_target(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _td_api, calls = _attach_gateway(broker)
    duplicate = _candidate_request()
    _authorize(broker, duplicate)

    with pytest.raises(ValueError, match="aggregate.*persisted intent"):
        _authorize(broker, duplicate, duplicate)

    assert broker._candidate_order_authorizations == {}
    with pytest.raises(RuntimeError, match="exact candidate request"):
        broker.send_order(duplicate)
    assert calls == []
    assert not path.exists()


def test_same_day_close_always_uses_independent_risk_reduction_authorization(
    tmp_path: Path,
) -> None:
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_gateway(broker)
    _authorize(broker, _candidate_request())

    order_id = broker.send_order(_reduction_request())

    entry = CtpOrderSubmissionJournal(path).get_entry(order_id)
    assert entry is not None
    assert entry.authorization_kind == "risk_reduction"
    assert entry.request == _reduction_request()


def test_fatal_callback_after_prepare_aborts_before_official_send(
    tmp_path: Path, monkeypatch
) -> None:
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _authorize(broker, _candidate_request())
    _td_api, calls = _attach_gateway(broker)
    original = broker._order_submission_journal.prepare

    def prepare_then_fatal(entry, **kwargs):
        prepared = original(entry, **kwargs)
        with broker._critical_callback_scope():
            broker._enqueue_critical(BrokerEvent("broker_error", "fatal after prepare"))
        return prepared

    monkeypatch.setattr(broker._order_submission_journal, "prepare", prepare_then_fatal)

    with pytest.raises(RuntimeError, match="acknowledged critical event boundary"):
        broker.send_order(_candidate_request())

    assert calls == []
    entry = CtpOrderSubmissionJournal(path).load_required().all_entries[-1]
    assert entry.status == "aborted_before_send"


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
