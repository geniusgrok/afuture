from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
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


def _rewrite_with_valid_checksum(path: Path, mutate) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutate(raw["current"])
    generation_unsigned = {key: value for key, value in raw["current"].items() if key != "checksum"}
    raw["current"]["checksum"] = hashlib.sha256(
        json.dumps(
            generation_unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    unsigned = {key: value for key, value in raw.items() if key != "checksum"}
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    raw["checksum"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")


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


def _persist_fill_evidence(
    broker: CtpBroker,
    order_id: str,
    trade_ids: tuple[str, ...],
) -> tuple[str, ...]:
    from afuture.broker.ctp_order_journal import ctp_order_fill_evidence

    assert broker._order_submission_journal is not None
    evidence = tuple(
        ctp_order_fill_evidence(
            "20260825",
            broker._convert_trade(_raw_trade(order_id, trade_id=trade_id)),
        )
        for trade_id in trade_ids
    )
    broker._order_submission_journal.apply_updates(fills={order_id: evidence})
    return tuple(item.key for item in evidence)


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


def test_late_session_query_ingress_aborts_prepared_order_before_official_send(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from threading import Event, Thread

    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal
    from afuture.broker.ctp_session_query import build_ctp_session_activity_evidence

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    broker.require_stress90_session_startup_capability()
    _td_api, calls = _attach_official_increment_boundary(broker)
    evidence = build_ctp_session_activity_evidence(
        account_identity_digest=broker.get_account_identity_digest(),
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=12,
        orders=(),
        trades=(),
        critical_generation=0,
        query_ingress_generation=0,
    )
    broker.install_stress90_session_startup_capability(
        evidence=evidence,
        ownership_digest="c" * 64,
    )
    entered = Event()
    release = Event()

    def block_impl(*_args, **_kwargs):
        entered.set()
        assert release.wait(1.0)

    monkeypatch.setattr(broker, "_handle_session_query_response_impl", block_impl)
    worker = Thread(
        target=broker._handle_session_query_response,
        args=("trade", None, {}, 12, True),
    )
    assert broker._order_submission_journal is not None
    original_prepare = broker._order_submission_journal.prepare

    def prepare_then_enter_query(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        worker.start()
        assert entered.wait(1.0)
        return prepared

    monkeypatch.setattr(
        broker._order_submission_journal,
        "prepare",
        prepare_then_enter_query,
    )
    try:
        with pytest.raises(RuntimeError, match="session query"):
            broker.send_order(_request())
    finally:
        release.set()
        worker.join(timeout=1.0)

    assert calls == []
    record = CtpOrderSubmissionJournal(path).load_required()
    assert record.entries[-1].status == "aborted_before_send"


def test_risk_reduction_without_candidate_context_gets_current_day_durable_intent(
    tmp_path: Path,
) -> None:
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    broker.configure_order_submission_journal(
        path,
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    _td_api, calls = _attach_official_increment_boundary(broker)

    order_id = broker.send_order(_close_request())

    assert order_id == "CTP.7_11_41"
    assert calls == [_close_request()]
    entry = CtpOrderSubmissionJournal(path).load_required().entries[-1]
    assert entry.target_trading_day == "20260825"
    assert entry.request == _close_request()
    assert entry.authorization_kind == "risk_reduction"


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


def test_reduction_only_context_can_never_authorize_an_open(tmp_path: Path) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    broker.configure_order_submission_journal(
        path,
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    _td_api, calls = _attach_official_increment_boundary(broker)

    with pytest.raises(RuntimeError, match="candidate context is missing"):
        broker.send_order(_request())

    assert calls == []


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


def test_order_submission_rejects_callback_conversion_in_flight(tmp_path: Path) -> None:
    broker = CtpBroker(_credentials())
    _configure(broker, tmp_path / "stress90_ctp_orders.json")
    _td_api, calls = _attach_official_increment_boundary(broker)
    entered = threading.Event()
    release = threading.Event()

    def critical_callback() -> None:
        with broker._critical_callback_scope():
            entered.set()
            release.wait(2.0)

    worker = threading.Thread(target=critical_callback)
    worker.start()
    try:
        assert entered.wait(1.0)
        with pytest.raises(RuntimeError, match="acknowledged critical event boundary"):
            broker.send_order(_request())
        assert calls == []
    finally:
        release.set()
        worker.join(2.0)
    assert not worker.is_alive()


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


def test_trade_callback_rejects_missing_timestamp_and_worse_than_limit_fill(
    tmp_path: Path,
) -> None:
    broker = CtpBroker(_credentials())
    _configure(broker, tmp_path / "stress90_ctp_orders.json")
    td_api, _calls = _attach_official_increment_boundary(broker)
    split_request = _request(volume=1)
    broker.authorize_candidate_order_requests((split_request, split_request))
    first_order = broker.send_order(split_request)
    td_api.order_ref = 41
    second_order = broker.send_order(split_request)

    broker._on_trade(SimpleNamespace(data=_raw_trade(first_order, timestamp=None)))
    broker._on_trade(SimpleNamespace(data=_raw_trade(second_order, trade_id="T2", price=1_002.0)))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error", "broker_error"]
    assert "timestamp" in str(events[0].payload)
    assert "limit price" in str(events[1].payload)
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


def test_rechecksummed_generation_cannot_rewrite_existing_fill_fingerprint(
    tmp_path: Path,
) -> None:
    from afuture.broker.ctp_order_journal import (
        CtpOrderJournalIntegrityError,
        CtpOrderSubmissionJournal,
    )

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    _persist_fill_evidence(broker, order_id, ("T1",))
    _persist_fill_evidence(broker, order_id, ("T2",))

    def rewrite_prior_fill_fingerprint(current: dict[str, object]) -> None:
        entries = current["entries"]
        assert isinstance(entries, list)
        entry = entries[0]
        assert isinstance(entry, dict)
        evidence = entry["fill_evidence"]
        assert isinstance(evidence, list)
        prior = evidence[0]
        assert isinstance(prior, dict)
        prior["price"] = 999.0

    _rewrite_with_valid_checksum(path, rewrite_prior_fill_fingerprint)

    with pytest.raises(CtpOrderJournalIntegrityError, match="fingerprint changed"):
        CtpOrderSubmissionJournal(path).load_required()


def test_restart_never_truncates_durable_fill_fingerprints(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    first_id = broker.send_order(_request())
    persisted = set(_persist_fill_evidence(broker, first_id, ("T1", "T2")))
    second_request = _request(volume=1)
    broker.authorize_candidate_order_requests((second_request,))
    second_id = broker.send_order(second_request)
    persisted.update(_persist_fill_evidence(broker, second_id, ("T3",)))
    assert len(persisted) == 3

    monkeypatch.setattr(CtpBroker, "_MAX_SEEN_TRADE_KEYS", 2)
    restarted = CtpBroker(_credentials())
    with pytest.raises(RuntimeError, match="exceed Broker replay capacity"):
        restarted.configure_order_submission_journal(
            path,
            policy_id="directional.stress90",
            policy_definition_digest="a" * 64,
            products_manifest_digest="b" * 64,
        )
    assert restarted._seen_trade_keys == set()
    assert restarted.get_positions() == []


def test_restart_rejects_persisted_orders_that_overreserve_fill_capacity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    broker.send_order(_request())

    monkeypatch.setattr(CtpBroker, "_MAX_SEEN_TRADE_KEYS", 1)
    restarted = CtpBroker(_credentials())
    with pytest.raises(RuntimeError, match="fill identity reservation"):
        restarted.configure_order_submission_journal(
            path,
            policy_id="directional.stress90",
            policy_definition_digest="a" * 64,
            products_manifest_digest="b" * 64,
        )
    assert restarted._order_submission_entries == {}
    assert restarted._seen_trade_keys == set()


def test_generic_state_seed_cannot_evict_durable_fill_fingerprints(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    durable = set(_persist_fill_evidence(broker, order_id, ("T1", "T2")))

    monkeypatch.setattr(CtpBroker, "_MAX_SEEN_TRADE_KEYS", 3)
    restarted = CtpBroker(_credentials())
    restarted.configure_order_submission_journal(
        path,
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    before = set(restarted._seen_trade_keys)
    with pytest.raises(RuntimeError, match="exceed Broker replay capacity"):
        restarted.seed_trade_identities(["20260825:DCE:STATE.X1", "20260825:DCE:STATE.X2"])
    assert restarted._seen_trade_keys == before == durable
    assert set(restarted._seen_trade_fingerprints) == durable


def test_full_durable_fill_window_rejects_new_trade_before_position_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    first_id = broker.send_order(_request())
    durable = set(_persist_fill_evidence(broker, first_id, ("T1", "T2")))
    second_request = _request(volume=1)
    broker.authorize_candidate_order_requests((second_request,))
    second_id = broker.send_order(second_request)

    restarted = CtpBroker(_credentials())
    restarted.configure_order_submission_journal(
        path,
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    monkeypatch.setattr(CtpBroker, "_MAX_SEEN_TRADE_KEYS", 2)
    restarted.get_trading_day = lambda: "20260825"

    restarted._on_trade(SimpleNamespace(data=_raw_trade(second_id, trade_id="T3")))

    events = restarted.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "capacity" in str(events[0].payload)
    assert restarted.get_positions() == []
    assert restarted._seen_trade_keys == durable


def test_sealed_epoch_fill_replay_halts_before_position_mutation(tmp_path: Path) -> None:
    from afuture.broker.ctp_order_journal import (
        CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
    )

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    _persist_fill_evidence(broker, order_id, ("T1",))
    assert broker._order_submission_journal is not None
    broker._order_submission_journal.update_status(order_id, "terminal")
    broker._order_submission_journal.compact_terminal()
    broker._order_submission_journal.seal_epoch(
        transaction_id="f" * 64,
        source_account_identity_digest=broker.get_account_identity_digest(),
        target_account_identity_digest=broker.get_account_identity_digest(),
        trading_day="20260825",
        operator_reason="full replay capacity rollover",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        strong_confirmation=CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
    )

    restarted = CtpBroker(_credentials())
    restarted.configure_order_submission_journal(
        path,
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    restarted.get_trading_day = lambda: "20260825"

    restarted._on_trade(SimpleNamespace(data=_raw_trade(order_id, trade_id="T1")))

    events = restarted.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "sealed account epoch" in str(events[0].payload)
    assert restarted.get_positions() == []


def test_submission_reserves_worst_case_fill_capacity_before_official_send(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _td_api, calls = _attach_official_increment_boundary(broker)
    first_id = broker.send_order(_request())
    _persist_fill_evidence(broker, first_id, ("T1",))
    assert broker._order_submission_journal is not None
    broker._order_submission_journal.update_status(first_id, "terminal")

    monkeypatch.setattr(CtpBroker, "_MAX_SEEN_TRADE_KEYS", 2)
    with pytest.raises(RuntimeError, match="fill identity capacity"):
        broker.send_order(_close_request())

    assert calls == [_request()]
    record = broker._order_submission_journal.load_required()
    assert [entry.order_id for entry in record.all_entries] == [first_id]


def test_long_running_broker_refreshes_to_bounded_recent_runtime_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_RUNTIME_RECENT_DAYS", 2)
    monkeypatch.setattr(journal_module, "_RUNTIME_RECENT_MAX_ENTRIES", 2)
    monkeypatch.setattr(CtpBroker, "_MAX_SEEN_TRADE_KEYS", 3)
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    broker.configure_order_submission_journal(
        path,
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    td_api, calls = _attach_official_increment_boundary(broker)
    current_day = ["20260821"]
    td_api.getTradingDay = lambda: current_day[0]
    for index, day in enumerate(
        ("20260821", "20260822", "20260823", "20260824", "20260825"),
        start=1,
    ):
        current_day[0] = day
        decision_digest = f"{index:x}" * 64
        request = replace(
            _request(volume=1),
            reference=f"directional:stress90:{decision_digest[:12]}:A",
        )
        broker.set_order_submission_context(
            target_trading_day=day,
            daily_decision_digest=decision_digest,
            execution_intent_digest=f"{index + 5:x}" * 64,
            transition={"freeze_authorized_lots": {"A2701": 1}, "transitions": []},
        )
        broker.authorize_candidate_order_requests((request,))
        order_id = broker.send_order(request)
        broker._on_trade(
            SimpleNamespace(
                data=_raw_trade(
                    order_id,
                    trade_id=f"T{index}",
                    volume=1,
                )
            )
        )
        assert [event.event_type for event in broker.poll_events()] == ["trade"]
        broker._pending_order_journal_status[order_id] = "terminal"
        broker.checkpoint_order_submission_journal()
        assert broker.acknowledge_critical_events() is True
        assert len(broker._order_submission_entries) <= 2
        assert len(broker._seen_trade_fingerprints) <= 2

    restarted = CtpBroker(_credentials())
    restarted.configure_order_submission_journal(
        path,
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    assert set(restarted._order_submission_entries) == set(broker._order_submission_entries)
    assert restarted._seen_trade_fingerprints == broker._seen_trade_fingerprints
    assert len(calls) == 5


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


def test_broker_runtime_configuration_never_uses_full_cold_archive_loader(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    assert broker._order_submission_journal is not None
    broker._order_submission_journal.update_status(order_id, "terminal")
    assert broker._order_submission_journal.compact_terminal() == 1

    monkeypatch.setattr(
        CtpOrderSubmissionJournal,
        "load",
        lambda _self: (_ for _ in ()).throw(AssertionError("full archive load")),
    )
    restarted = CtpBroker(_credentials())
    _configure(restarted, path)

    assert restarted.owns_order(order_id)


def test_cold_start_checkpoint_treats_never_created_journal_as_empty(
    tmp_path: Path,
) -> None:
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    assert CtpOrderSubmissionJournal(path).compact_terminal() == 0

    broker = CtpBroker(_credentials())
    _configure(broker, path)
    broker.checkpoint_order_submission_journal()

    assert not path.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "M2701"),
        ("exchange", SimpleNamespace(value="SHFE")),
        ("direction", SimpleNamespace(name="SHORT")),
        ("offset", SimpleNamespace(name="CLOSE")),
        ("type", SimpleNamespace(name="LIMIT")),
        ("volume", 3),
        ("price", 1_002.0),
    ],
)
def test_exact_owned_callback_must_match_every_persisted_request_field(
    tmp_path: Path, field: str, value: object
):
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
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
        vt_orderid=order_id,
        symbol="A2701",
        exchange=SimpleNamespace(value="DCE"),
        direction=SimpleNamespace(name="LONG"),
        offset=SimpleNamespace(name="OPEN"),
        type=SimpleNamespace(name="FAK"),
        status="not_traded",
        volume=2,
        traded=0,
        price=1_001.0,
        reference="",
    )
    setattr(raw, field, value)

    broker._on_order(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "durable CTP order request mismatch" in str(events[0].payload)


@pytest.mark.parametrize("durable_status", ["terminal", "aborted_before_send"])
def test_active_callback_cannot_regress_from_durable_final_status(
    tmp_path: Path,
    durable_status: str,
) -> None:
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    broker._order_submission_entries[order_id] = replace(
        broker._order_submission_entries[order_id],
        status=durable_status,
    )
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
        vt_orderid=order_id,
        symbol="A2701",
        exchange=SimpleNamespace(value="DCE"),
        direction=SimpleNamespace(name="LONG"),
        offset=SimpleNamespace(name="OPEN"),
        type=SimpleNamespace(name="FAK"),
        status="not_traded",
        volume=2,
        traded=0,
        price=1_001.0,
        reference="",
    )

    broker._on_order(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "durable final" in str(events[0].payload)


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


def test_missing_current_order_journal_with_prev_evidence_fails_closed(tmp_path: Path):
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    broker.send_order(_request())
    assert path.with_name(path.name + ".prev").exists()
    path.unlink()

    with pytest.raises(RuntimeError, match="current.*missing"):
        _configure(CtpBroker(_credentials()), path)


def test_order_journal_prev_is_bound_to_current_parent_checksum_and_sequence(tmp_path: Path):
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    split_request = _request(volume=1)
    broker.authorize_candidate_order_requests((split_request, split_request))
    broker.send_order(split_request)
    older_valid = path.read_bytes()
    broker.send_order(split_request)
    path.with_name(path.name + ".prev").write_bytes(older_valid)

    with pytest.raises(RuntimeError, match="parent|sequence"):
        CtpOrderSubmissionJournal(path).load_required()


def test_order_journal_rejects_duplicate_exact_identity_even_with_valid_checksum(tmp_path: Path):
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    broker.send_order(_request())

    def duplicate(raw) -> None:
        duplicate_entry = dict(raw["entries"][0])
        duplicate_entry["sequence"] = 2
        raw["entries"].append(duplicate_entry)

    _rewrite_with_valid_checksum(path, duplicate)
    with pytest.raises(RuntimeError, match="duplicate.*identity"):
        CtpOrderSubmissionJournal(path).load_required()


def test_order_journal_rejects_inconsistent_entry_policy_identity(tmp_path: Path):
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    broker.send_order(_request())

    def add_inconsistent(raw) -> None:
        inconsistent = dict(raw["entries"][0])
        inconsistent["sequence"] = 2
        inconsistent["order_ref"] = 42
        inconsistent["order_id"] = "CTP.7_11_42"
        inconsistent["account_identity_digest"] = "c" * 64
        raw["entries"].append(inconsistent)

    _rewrite_with_valid_checksum(path, add_inconsistent)
    with pytest.raises(RuntimeError, match="inconsistent entry identity"):
        CtpOrderSubmissionJournal(path).load_required()


def test_terminal_order_journal_status_cannot_regress_to_submitted(tmp_path: Path):
    from afuture.broker.ctp_order_journal import (
        CtpOrderJournalIntegrityError,
        CtpOrderSubmissionJournal,
    )

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    journal = CtpOrderSubmissionJournal(path)
    journal.update_status(order_id, "terminal")

    with pytest.raises(CtpOrderJournalIntegrityError, match="status transition"):
        journal.update_status(order_id, "submitted")


def test_terminal_checkpoint_serializes_with_new_submission(tmp_path: Path, monkeypatch):
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    broker._pending_order_journal_status[order_id] = "terminal"
    entered_update = threading.Event()
    original = broker._order_submission_journal.apply_updates

    def track_update(*, statuses=None, fills=None):
        entered_update.set()
        return original(statuses=statuses, fills=fills)

    monkeypatch.setattr(broker._order_submission_journal, "apply_updates", track_update)
    broker._order_submission_lock.acquire()
    worker = threading.Thread(target=broker.checkpoint_order_submission_journal)
    try:
        worker.start()
        assert not entered_update.wait(0.1)
    finally:
        broker._order_submission_lock.release()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert entered_update.is_set()


def test_order_journal_explicit_bound_fails_closed_before_unbounded_growth(
    tmp_path: Path, monkeypatch
):
    import afuture.broker.ctp_order_journal as journal_module

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    td_api, _calls = _attach_official_increment_boundary(broker)
    monkeypatch.setattr(journal_module, "_MAX_ENTRIES", 1)
    split_request = _request(volume=1)
    broker.authorize_candidate_order_requests((split_request, split_request))
    broker.send_order(split_request)
    td_api.order_ref = 41

    with pytest.raises(RuntimeError, match="entry limit"):
        broker.send_order(split_request)


def test_crash_after_prepared_before_gateway_call_restarts_as_exact_owned(tmp_path: Path):
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)

    def crash() -> None:
        raise RuntimeError("injected crash before official call")

    _td_api, calls = _attach_official_increment_boundary(broker, before_call=crash)
    with pytest.raises(RuntimeError, match="before official call"):
        broker.send_order(_request())
    assert calls == []

    restarted = CtpBroker(_credentials())
    _configure(restarted, path)
    assert restarted.owns_order("CTP.7_11_41")
    assert [entry.order_id for entry in restarted.get_unresolved_order_submission_identities()] == [
        "CTP.7_11_41"
    ]


def test_crash_after_gateway_call_before_submitted_mark_restarts_as_exact_owned(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _td_api, calls = _attach_official_increment_boundary(broker)

    original = broker._order_submission_journal.update_status

    def crash_on_submitted(order_id: str, status: str) -> None:
        if status == "submitted":
            raise RuntimeError("injected crash before submitted mark")
        original(order_id, status)

    monkeypatch.setattr(broker._order_submission_journal, "update_status", crash_on_submitted)
    with pytest.raises(RuntimeError, match="submitted mark"):
        broker.send_order(_request())
    assert calls == [_request()]

    restarted = CtpBroker(_credentials())
    _configure(restarted, path)
    assert restarted.owns_order("CTP.7_11_41")


@pytest.mark.parametrize("terminal_status", ["filled", "rejected"])
def test_terminal_and_rejected_replay_checkpoint_exact_journal_outside_callback(
    tmp_path: Path,
    monkeypatch,
    terminal_status: str,
):
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal

    path = tmp_path / "stress90_ctp_orders.json"
    broker = CtpBroker(_credentials())
    _configure(broker, path)
    _attach_official_increment_boundary(broker)
    order_id = broker.send_order(_request())
    statuses = SimpleNamespace(
        SUBMITTING="submitting",
        NOTTRADED="not_traded",
        PARTTRADED="part_traded",
        ALLTRADED="filled",
        CANCELLED="cancelled",
        REJECTED="rejected",
    )
    broker._runtime = {"Status": statuses}
    raw = SimpleNamespace(
        vt_orderid=order_id,
        symbol="A2701",
        exchange=SimpleNamespace(value="DCE"),
        direction=SimpleNamespace(name="LONG"),
        offset=SimpleNamespace(name="OPEN"),
        type=SimpleNamespace(name="FAK"),
        status=terminal_status,
        volume=2,
        traded=2 if terminal_status == "filled" else 0,
        price=1_001.0,
        reference="",
    )
    writes: list[tuple[str, str]] = []
    original = broker._order_submission_journal.apply_updates

    def track(*, statuses=None, fills=None):
        writes.extend(sorted((statuses or {}).items()))
        return original(statuses=statuses, fills=fills)

    monkeypatch.setattr(broker._order_submission_journal, "apply_updates", track)
    broker._on_order(SimpleNamespace(data=raw))
    assert writes == []
    assert broker.poll_events()[0].payload.request.reference == _request().reference

    broker.checkpoint_order_submission_journal()
    assert writes == [(order_id, "terminal")]
    assert CtpOrderSubmissionJournal(path).load_required().all_entries[-1].status == "terminal"


def test_tracked_vnpy_ctp_adapter_consumes_reserved_ref_exactly_once(monkeypatch):
    """Exercise the real TrackedCtpTdApi subclass boundary, not only MainEngine fakes."""

    import sys

    class OfficialCtpTdApi:
        def __init__(self, gateway):
            self.gateway = gateway
            self.order_ref = 40
            self.frontid = 7
            self.sessionid = 11
            self.inserted = []

        def send_order(self, req):
            self.order_ref += 1
            payload = {"OrderRef": str(self.order_ref), "request": req}
            self.inserted.append(payload)
            return f"{self.frontid}_{self.sessionid}_{self.order_ref}"

    class OfficialCtpGateway:
        def __init__(self, event_engine, gateway_name):
            self.event_engine = event_engine
            self.gateway_name = gateway_name
            self.td_api = OfficialCtpTdApi(self)

    class OfficialCtpMdApi:
        def __init__(self, gateway):
            self.gateway = gateway

    modules = {
        "vnpy": ModuleType("vnpy"),
        "vnpy.event": ModuleType("vnpy.event"),
        "vnpy.trader": ModuleType("vnpy.trader"),
        "vnpy.trader.constant": ModuleType("vnpy.trader.constant"),
        "vnpy.trader.engine": ModuleType("vnpy.trader.engine"),
        "vnpy.trader.event": ModuleType("vnpy.trader.event"),
        "vnpy.trader.object": ModuleType("vnpy.trader.object"),
        "vnpy_ctp": ModuleType("vnpy_ctp"),
        "vnpy_ctp.gateway": ModuleType("vnpy_ctp.gateway"),
        "vnpy_ctp.gateway.ctp_gateway": ModuleType("vnpy_ctp.gateway.ctp_gateway"),
    }
    modules["vnpy.event"].EventEngine = object
    constant = modules["vnpy.trader.constant"]
    for name in ("Direction", "Exchange", "Status", "Offset", "OrderType"):
        setattr(constant, name, type(name, (), {}))
    modules["vnpy.trader.engine"].MainEngine = object
    event = modules["vnpy.trader.event"]
    event.EVENT_ACCOUNT = "account"
    event.EVENT_ORDER = "order"
    event.EVENT_TICK = "tick"
    event.EVENT_TRADE = "trade"
    modules["vnpy.trader.object"].OrderRequest = object
    modules["vnpy.trader.object"].SubscribeRequest = object
    ctp_gateway = modules["vnpy_ctp.gateway.ctp_gateway"]
    ctp_gateway.CtpGateway = OfficialCtpGateway
    ctp_gateway.CtpMdApi = OfficialCtpMdApi
    ctp_gateway.CtpTdApi = OfficialCtpTdApi
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    runtime = CtpBroker(_credentials())._load_runtime()
    gateway = runtime["CtpGateway"](object(), "CTP")
    td_api = gateway.td_api
    td_api._afuture_expected_order_ref = 41

    assert td_api.send_order("request") == "7_11_41"
    assert td_api.order_ref == 41
    assert td_api.inserted == [{"OrderRef": "41", "request": "request"}]

    td_api._afuture_expected_order_ref = 43
    with pytest.raises(RuntimeError, match="reserved CTP OrderRef"):
        td_api.send_order("must-not-send")
    assert len(td_api.inserted) == 1
