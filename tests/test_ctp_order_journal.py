from __future__ import annotations

from dataclasses import replace
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


def _request(*, volume: int = 2) -> OrderRequest:
    return OrderRequest(
        "A2701",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        volume,
        1_001.0,
        OrderType.FAK,
        "directional:stress90:" + "d" * 12 + ":A",
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
    broker.authorize_candidate_order_requests((_request(),))


def _attach_official_increment_boundary(
    broker: CtpBroker,
    *,
    before_call=None,
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
        if before_call is not None:
            before_call()
        # This is the official vnpy_ctp boundary: it increments exactly once itself.
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


def _close_request() -> OrderRequest:
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


def _raw_trade(
    order_id: str,
    *,
    trade_id: str = "T1",
    symbol: str = "A2701",
    exchange: str = "DCE",
    direction: str = "LONG",
    offset: str = "OPEN",
    volume: int = 1,
    price: float = 1_000.0,
    timestamp: datetime | None = datetime(
        2026,
        8,
        25,
        9,
        0,
        tzinfo=_CHINA,
    ),
) -> SimpleNamespace:
    return SimpleNamespace(
        vt_tradeid=f"CTP.{trade_id}",
        vt_orderid=order_id,
        symbol=symbol,
        exchange=SimpleNamespace(value=exchange),
        direction=SimpleNamespace(name=direction),
        offset=SimpleNamespace(name=offset),
        volume=volume,
        price=price,
        datetime=timestamp,
    )


def test_query_recovery_exposes_exact_session_trade_and_order_for_engine_adoption(
    tmp_path: Path,
) -> None:
    from afuture.broker.ctp_order_journal import (
        CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
    )
    from afuture.broker.ctp_session_query import (
        build_ctp_session_activity_evidence,
        normalize_ctp_session_order,
        normalize_ctp_session_trade,
    )

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    td_api, _calls = _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    order_ref = td_api.order_ref
    identity = {
        "broker_id": "9999",
        "investor_id": "user",
        "invest_unit_id": "",
        "trading_day": "20260825",
    }
    order = normalize_ctp_session_order(
        {
            "BrokerID": "9999",
            "InvestorID": "user",
            "InvestUnitID": "",
            "TradingDay": "20260825",
            "InstrumentID": "A2701",
            "ExchangeID": "DCE",
            "OrderSysID": "sys-41",
            "FrontID": 7,
            "SessionID": 11,
            "OrderRef": str(order_ref),
            "OrderStatus": "0",
            "Direction": "0",
            "CombOffsetFlag": "0",
            "VolumeTotalOriginal": 2,
            "VolumeTraded": 2,
            "VolumeTotal": 0,
            "LimitPrice": 1001.0,
            "OrderPriceType": "2",
            "TimeCondition": "1",
            "VolumeCondition": "1",
        },
        **identity,
    )
    trade = normalize_ctp_session_trade(
        {
            "BrokerID": "9999",
            "InvestorID": "user",
            "InvestUnitID": "",
            "TradingDay": "20260825",
            "InstrumentID": "A2701",
            "ExchangeID": "DCE",
            "TradeID": "T-query",
            "OrderSysID": "sys-41",
            "OrderRef": str(order_ref),
            "Direction": "0",
            "OffsetFlag": "0",
            "Volume": 2,
            "Price": 1000.0,
            "TradeDate": "20260825",
            "TradeTime": "09:01:02",
        },
        **identity,
    )
    evidence = build_ctp_session_activity_evidence(
        account_identity_digest=broker.get_account_identity_digest(),
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=12,
        orders=(order,),
        trades=(trade,),
        critical_generation=0,
    )
    broker.require_session_activity_evidence_current = lambda _evidence: None
    broker._main_engine = SimpleNamespace(
        get_all_trades=lambda: [],
        get_order=lambda _order_id: None,
    )

    assert broker.recover_stress90_session_activity(evidence) == ()

    recovered = broker.get_session_trades()
    assert [(item.trade_id, item.order_id, item.volume) for item in recovered] == [
        ("CTP.T-query", order_id, 2)
    ]
    assert broker.get_order(order_id) is not None
    assert broker.get_order(order_id).request == _request()

    assert broker._order_submission_journal is not None
    broker._order_submission_journal.compact_terminal()
    broker._order_submission_journal.seal_epoch(
        transaction_id="f" * 64,
        source_account_identity_digest=broker.get_account_identity_digest(),
        target_account_identity_digest=broker.get_account_identity_digest(),
        trading_day="20260825",
        operator_reason="same-day recovery must not replay sealed fills",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        strong_confirmation=CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
    )

    assert broker.recover_stress90_session_activity(evidence) == ()
    assert broker.get_session_trades() == []
    assert broker.get_order(order_id) is None


def test_exact_numeric_order_ref_is_durable_before_official_send_call(tmp_path: Path):
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)

    def assert_prepared() -> None:
        from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

        record = CtpOrderSubmissionJournal(path).load_required()
        entry = record.entries[-1]
        assert entry.status == "prepared"
        assert (entry.front_id, entry.session_id, entry.order_ref) == (7, 11, 41)
        assert entry.order_id == "CTP.7_11_41"
        assert entry.account_identity_digest == broker.get_account_identity_digest()
        assert entry.request == _request()
        assert entry.transition["transitions"][0]["kind"] == "same_product_roll"

    td_api, calls = _attach_official_increment_boundary(broker, before_call=assert_prepared)
    order_id = broker.send_order(_request())

    assert order_id == "CTP.7_11_41"
    assert td_api.order_ref == 41
    assert calls == [_request()]
    assert broker.owns_order(order_id)


def test_order_journal_persistence_failure_attempts_zero_ctp_sends(tmp_path: Path, monkeypatch):
    broker = CtpBroker(_credentials())
    _configure(broker, tmp_path / "stress90_ctp_orders.json")
    _td_api, calls = _attach_official_increment_boundary(broker)

    def fail_prepare(*_args, **_kwargs):
        raise RuntimeError("injected journal fsync failure")

    monkeypatch.setattr(broker._order_submission_journal, "prepare", fail_prepare)
    with pytest.raises(RuntimeError, match="journal fsync"):
        broker.send_order(_request())
    assert calls == []


def test_stale_candidate_context_never_binds_current_day_open_or_reduction(
    tmp_path: Path,
) -> None:
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    broker._order_submission_context = replace(
        broker._order_submission_context,
        target_trading_day="20260824",
    )
    _td_api, calls = _attach_official_increment_boundary(broker)

    with pytest.raises(RuntimeError, match="current CTP trading day"):
        broker.send_order(_request())
    assert calls == []

    close_id = broker.send_order(_close_request())
    assert close_id == "CTP.7_11_41"
    entry = CtpOrderSubmissionJournal(path).load_required().entries[-1]
    assert entry.target_trading_day == "20260825"
    assert entry.authorization_kind == "risk_reduction"


def test_order_submission_requires_consumed_and_acknowledged_critical_fifo(
    tmp_path: Path,
) -> None:
    broker = CtpBroker(_credentials())
    _configure(broker, tmp_path / "stress90_ctp_orders.json")
    _td_api, calls = _attach_official_increment_boundary(broker)
    broker._enqueue_critical(BrokerEvent("broker_error", "fatal account drift"))

    with pytest.raises(RuntimeError, match="acknowledged critical event boundary"):
        broker.send_order(_request())
    assert [event.event_type for event in broker.poll_events()] == ["broker_error"]
    with pytest.raises(RuntimeError, match="acknowledged critical event boundary"):
        broker.send_order(_request())

    assert broker.acknowledge_critical_events() is True
    assert broker.send_order(_request()) == "CTP.7_11_41"
    assert calls == [_request()]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "M2701"),
        ("exchange", "SHFE"),
        ("direction", "SHORT"),
        ("offset", "CLOSE"),
    ],
)
def test_trade_callback_must_match_durable_request_before_position_mutation(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    broker = CtpBroker(_credentials())
    _configure(broker, tmp_path / "stress90_ctp_orders.json")
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    raw = _raw_trade(order_id)
    if field in {"exchange", "direction", "offset"}:
        converted = (
            SimpleNamespace(value=value) if field == "exchange" else SimpleNamespace(name=value)
        )
        setattr(raw, field, converted)
    else:
        setattr(raw, field, value)

    broker._on_trade(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "durable CTP trade request mismatch" in str(events[0].payload)
    assert broker.get_positions() == []


def test_trade_cumulative_volume_is_bounded_and_checkpointed_once_per_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    writes = 0
    original = broker._order_submission_journal.apply_updates

    def track_updates(*args, **kwargs):
        nonlocal writes
        writes += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(broker._order_submission_journal, "apply_updates", track_updates)
    for index in range(1, 4):
        broker._on_trade(SimpleNamespace(data=_raw_trade(order_id, trade_id=f"T{index}")))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["trade", "trade", "broker_error"]
    assert broker.get_positions()[0].long_today == 2
    assert writes == 0

    broker.checkpoint_order_submission_journal()

    assert writes == 1
    entry = CtpOrderSubmissionJournal(path).load_required().entries[-1]
    assert entry.filled_volume == 2
    assert entry.fill_keys == ("20260825:DCE:CTP.T1", "20260825:DCE:CTP.T2")


def test_persisted_fill_keys_and_volume_are_reused_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    first = CtpBroker(_credentials())
    _configure(first, path)
    _attach_official_increment_boundary(first)
    order_id = first.send_order(_request())
    first._on_trade(SimpleNamespace(data=_raw_trade(order_id)))
    assert [event.event_type for event in first.poll_events()] == ["trade"]
    first.checkpoint_order_submission_journal()

    restarted = CtpBroker(_credentials())
    _configure(restarted, path)
    restarted._trading_day = "20260825"
    restarted._on_trade(SimpleNamespace(data=_raw_trade(order_id)))
    assert restarted.poll_events() == []
    restarted._on_trade(SimpleNamespace(data=_raw_trade(order_id, trade_id="T2")))
    restarted._on_trade(SimpleNamespace(data=_raw_trade(order_id, trade_id="T3")))

    events = restarted.poll_events()
    assert [event.event_type for event in events] == ["trade", "broker_error"]
    assert restarted.get_positions()[0].long_today == 1


def test_restart_rejects_duplicate_fill_identity_with_changed_economics(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    first = CtpBroker(_credentials())
    _configure(first, path)
    _attach_official_increment_boundary(first)
    order_id = first.send_order(_request())
    first._on_trade(SimpleNamespace(data=_raw_trade(order_id, volume=1)))
    assert [event.event_type for event in first.poll_events()] == ["trade"]
    first.checkpoint_order_submission_journal()

    restarted = CtpBroker(_credentials())
    _configure(restarted, path)
    restarted._trading_day = "20260825"
    restarted._on_trade(SimpleNamespace(data=_raw_trade(order_id, volume=2)))

    events = restarted.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "fingerprint" in str(events[0].payload)
    assert restarted.get_positions() == []


def test_restart_restores_reference_only_by_exact_ctp_identity_and_not_prefix(tmp_path: Path):
    path = tmp_path / "stress90_ctp_orders.json"
    first = CtpBroker(_credentials())
    _configure(first, path)
    _attach_official_increment_boundary(first)
    exact_id = first.send_order(_request())

    restarted = CtpBroker(_credentials())
    _configure(restarted, path)
    assert restarted.owns_order(exact_id)
    assert not restarted.owns_order("CTP.7_11_999")

    restarted._runtime = {
        "Status": SimpleNamespace(
            SUBMITTING="submitting",
            NOTTRADED="not_traded",
            PARTTRADED="part_traded",
            ALLTRADED="filled",
            CANCELLED="cancelled",
            REJECTED="rejected",
        )
    }
    raw = SimpleNamespace(
        vt_orderid=exact_id,
        symbol="A2701",
        exchange=SimpleNamespace(value="DCE"),
        direction=SimpleNamespace(name="LONG"),
        offset=SimpleNamespace(name="OPEN"),
        type=SimpleNamespace(name="FAK"),
        status="part_traded",
        volume=2,
        traded=1,
        price=1_001.0,
        # Real vnpy_ctp replay does not populate reference.
        reference="",
    )
    assert restarted._convert_order(raw).request.reference == _request().reference


def test_cold_or_unknown_order_callback_fails_before_manager_mutation(
    tmp_path: Path,
) -> None:
    broker = CtpBroker(_credentials())
    _configure(broker, tmp_path / "stress90_ctp_orders.json")
    broker._runtime = {
        "Status": SimpleNamespace(
            SUBMITTING="submitting",
            NOTTRADED="not_traded",
            PARTTRADED="part_traded",
            ALLTRADED="filled",
            CANCELLED="cancelled",
            REJECTED="rejected",
        )
    }
    raw = SimpleNamespace(
        vt_orderid="CTP.1_2_OLD",
        symbol="A2701",
        exchange=SimpleNamespace(value="DCE"),
        direction=SimpleNamespace(name="LONG"),
        offset=SimpleNamespace(name="OPEN"),
        type=SimpleNamespace(name="FAK"),
        status="not_traded",
        volume=2,
        traded=0,
        price=1_001.0,
        reference="manual-or-cold",
    )

    broker._on_order(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "no bounded durable" in str(events[0].payload)


def test_current_order_journal_corruption_never_falls_back_to_prev(tmp_path: Path):
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    split_request = _request(volume=1)
    broker.authorize_candidate_order_requests((split_request, split_request))
    broker.send_order(split_request)
    broker.send_order(split_request)
    assert path.with_name(path.name + ".prev").exists()
    path.write_text("{corrupt", encoding="utf-8")

    restarted = CtpBroker(_credentials())
    with pytest.raises(RuntimeError, match="journal JSON"):
        _configure(restarted, path)
