from __future__ import annotations

from copy import deepcopy
from threading import Event, Thread

import pytest


def _row(
    symbol: str,
    *,
    product_class: str = "1",
    exchange: str = "DCE",
    product: str = "A",
    expiry: str = "20261215",
    listing: str = "20260101",
) -> dict[str, object]:
    return {
        "InstrumentID": symbol,
        "ExchangeID": exchange,
        "ProductID": product,
        "ExpireDate": expiry,
        "OpenDate": listing,
        "ProductClass": product_class,
    }


def test_catalog_publishes_only_at_complete_response_boundary_and_filters_non_futures():
    from afuture.broker.ctp_catalog import CtpContractCatalogAccumulator

    catalog = CtpContractCatalogAccumulator()
    catalog.begin(request_id=11, trading_day="20260825")

    assert catalog.observe(11, _row("a2612"), {}, last=False) is None
    assert catalog.latest_verified is None
    assert (
        catalog.observe(
            11,
            _row("A2612-C-3000", product_class="2"),
            {},
            last=False,
        )
        is None
    )
    snapshot = catalog.observe(
        11,
        _row("SP a2612&a2701", product_class="3"),
        {},
        last=True,
    )

    assert snapshot is not None
    assert snapshot.trading_day == "20260825"
    assert snapshot.request_id == 11
    assert snapshot.generation == 1
    assert [item.symbol for item in snapshot.contracts] == ["a2612"]
    assert snapshot.filtered_non_futures == 2
    assert catalog.latest_verified == snapshot


def test_catalog_digest_is_independent_of_callback_row_order():
    from afuture.broker.ctp_catalog import CtpContractCatalogAccumulator

    rows = [
        _row("a2612"),
        _row("m2612", product="M"),
        _row("TA612", product="TA", exchange="CZCE"),
    ]
    first = CtpContractCatalogAccumulator()
    second = CtpContractCatalogAccumulator()
    first.begin(request_id=1, trading_day="20260825")
    second.begin(request_id=99, trading_day="20260825")
    for index, row in enumerate(rows):
        first_snapshot = first.observe(1, row, {}, last=index == len(rows) - 1)
    for index, row in enumerate(reversed(rows)):
        second_snapshot = second.observe(99, row, {}, last=index == len(rows) - 1)

    assert first_snapshot is not None
    assert second_snapshot is not None
    assert first_snapshot.catalog_digest == second_snapshot.catalog_digest
    assert first_snapshot.contracts == second_snapshot.contracts


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda row: row.pop("ProductClass"), "ProductClass"),
        (lambda row: row.__setitem__("ExpireDate", "bad"), "lifecycle"),
        (lambda row: row.__setitem__("ProductID", "M"), "identity"),
    ],
)
def test_invalid_futures_row_fails_generation_without_replacing_verified_cache(mutate, reason):
    from afuture.broker.ctp_catalog import (
        CtpContractCatalogAccumulator,
        CtpContractCatalogIntegrityError,
    )

    catalog = CtpContractCatalogAccumulator()
    catalog.begin(request_id=1, trading_day="20260825")
    verified = catalog.observe(1, _row("a2612"), {}, last=True)
    bad = deepcopy(_row("a2701"))
    mutate(bad)
    catalog.begin(request_id=2, trading_day="20260826")

    with pytest.raises(CtpContractCatalogIntegrityError, match=reason):
        catalog.observe(2, bad, {}, last=True)

    assert catalog.latest_verified == verified
    assert catalog.last_failure is not None
    assert catalog.last_failure.trading_day == "20260826"


def test_duplicate_or_error_response_never_partially_replaces_catalog():
    from afuture.broker.ctp_catalog import (
        CtpContractCatalogAccumulator,
        CtpContractCatalogIntegrityError,
    )

    catalog = CtpContractCatalogAccumulator()
    catalog.begin(request_id=1, trading_day="20260825")
    verified = catalog.observe(1, _row("a2612"), {}, last=True)

    catalog.begin(request_id=2, trading_day="20260826")
    catalog.observe(2, _row("a2701"), {}, last=False)
    with pytest.raises(CtpContractCatalogIntegrityError, match="duplicate"):
        catalog.observe(2, _row("a2701"), {}, last=True)
    assert catalog.latest_verified == verified

    catalog.begin(request_id=3, trading_day="20260826")
    with pytest.raises(CtpContractCatalogIntegrityError, match="query failed"):
        catalog.observe(
            3,
            None,
            {"ErrorID": 7, "ErrorMsg": "not ready"},
            last=True,
        )
    assert catalog.latest_verified == verified


def test_catalog_rejects_overlapping_or_stale_request_boundaries():
    from afuture.broker.ctp_catalog import (
        CtpContractCatalogAccumulator,
        CtpContractCatalogIntegrityError,
    )

    catalog = CtpContractCatalogAccumulator()
    catalog.begin(request_id=10, trading_day="20260825")
    with pytest.raises(CtpContractCatalogIntegrityError, match="already active"):
        catalog.begin(request_id=11, trading_day="20260825")
    with pytest.raises(CtpContractCatalogIntegrityError, match="request id"):
        catalog.observe(9, _row("a2612"), {}, last=True)


def test_catalog_batch_has_explicit_memory_bound():
    from afuture.broker.ctp_catalog import (
        MAX_CTP_CATALOG_ROWS,
        CtpContractCatalogAccumulator,
        CtpContractCatalogIntegrityError,
    )

    catalog = CtpContractCatalogAccumulator(max_rows=2)
    catalog.begin(request_id=1, trading_day="20260825")
    catalog.observe(1, _row("a2612"), {}, last=False)
    catalog.observe(1, _row("m2612", product="M"), {}, last=False)
    with pytest.raises(CtpContractCatalogIntegrityError, match="memory bound"):
        catalog.observe(1, _row("p2612", product="P"), {}, last=True)

    assert MAX_CTP_CATALOG_ROWS >= 5_000
    assert catalog.latest_verified is None


def test_ctp_broker_publishes_catalog_only_at_matching_complete_boundary():
    from afuture.broker.ctp import CtpBroker, CtpCredentials

    broker = CtpBroker(CtpCredentials("u", "p", "b", "td", "md", "", ""))
    broker._trading_day = "20260825"

    broker._handle_contract_catalog_response(_row("a2612"), {}, 11, False)
    assert broker.get_contract_catalog() == []
    broker._handle_contract_catalog_response(
        _row("A2612-C-3000", product_class="2"),
        {},
        11,
        True,
    )

    catalog = broker.get_contract_catalog()
    assert [(item.symbol, item.product) for item in catalog] == [("a2612", "A")]
    status = broker.get_contract_catalog_status()
    assert status["trading_day"] == "20260825"
    assert status["request_id"] == 11
    assert status["generation"] == 1
    assert len(status["digest"]) == 64


def test_ctp_catalog_callback_blocks_critical_ack_while_conversion_is_in_flight(
    tmp_path,
    monkeypatch,
):
    from afuture.broker.ctp import CtpBroker, CtpCredentials

    broker = CtpBroker(CtpCredentials("u", "p", "b", "td", "md", "", ""))
    broker._trading_day = "20260825"
    broker.configure_order_submission_journal(
        tmp_path / "stress90_ctp_orders.json",
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    entered = Event()
    release = Event()
    original = broker._contract_catalog_accumulator.observe

    def blocking_observe(*args, **kwargs):
        entered.set()
        assert release.wait(2.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(broker._contract_catalog_accumulator, "observe", blocking_observe)
    worker = Thread(
        target=broker._handle_contract_catalog_response,
        args=(_row("a2612"), {}, 11, True),
    )
    worker.start()
    try:
        assert entered.wait(1.0)
        assert broker.acknowledge_critical_events() is False
        assert broker.has_pending_critical_events() is True
    finally:
        release.set()
        worker.join(2.0)
    assert not worker.is_alive()
    assert broker.acknowledge_critical_events() is True


def test_ctp_broker_failed_refresh_retains_prior_verified_catalog_and_surfaces_failure():
    from afuture.broker.ctp import CtpBroker, CtpCredentials

    broker = CtpBroker(CtpCredentials("u", "p", "b", "td", "md", "", ""))
    broker._trading_day = "20260825"
    broker._handle_contract_catalog_response(_row("a2612"), {}, 1, True)
    verified = broker.get_contract_catalog()
    broker._begin_contract_catalog_generation(
        request_id=2,
        trading_day="20260825",
    )

    broker._handle_contract_catalog_response(
        None,
        {"ErrorID": 7, "ErrorMsg": "query unavailable"},
        2,
        True,
    )

    assert broker.get_contract_catalog() == verified
    assert broker.contract_catalog_verified_for_day("20260825") is True
    assert broker.poll_events() == []
    assert "catalog query failed" in str(broker.get_contract_catalog_status()["last_failure"])


def test_ctp_broker_rejects_stale_catalog_callback_without_partial_replace():
    from afuture.broker.ctp import CtpBroker, CtpCredentials

    broker = CtpBroker(CtpCredentials("u", "p", "b", "td", "md", "", ""))
    broker._trading_day = "20260825"
    broker._handle_contract_catalog_response(_row("a2612"), {}, 1, False)
    broker._handle_contract_catalog_response(_row("m2612", product="M"), {}, 2, True)

    assert broker.get_contract_catalog() == []
    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "request id" in str(events[0].payload)


def test_ctp_catalog_aborted_generation_cannot_be_resurrected_by_late_last():
    from afuture.broker.ctp import CtpBroker, CtpCredentials

    broker = CtpBroker(CtpCredentials("u", "p", "b", "td", "md", "", ""))
    broker._trading_day = "20260825"
    broker._handle_contract_catalog_response(_row("a2612"), {}, 77, False)
    broker._contract_catalog_accumulator.abort(
        request_id=77,
        reason="catalog query timeout",
    )

    broker._handle_contract_catalog_response(_row("a2701"), {}, 77, True)

    assert broker.get_contract_catalog() == []
    assert broker.contract_catalog_verified_for_day("20260825") is False
    assert broker.get_contract_catalog_status()["last_failure"] == "catalog query timeout"
    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "not registered" in str(events[0].payload)


def test_ctp_catalog_completed_generation_rejects_duplicate_last_without_republish():
    from afuture.broker.ctp import CtpBroker, CtpCredentials

    broker = CtpBroker(CtpCredentials("u", "p", "b", "td", "md", "", ""))
    broker._trading_day = "20260825"
    broker._handle_contract_catalog_response(_row("a2612"), {}, 11, True)
    original = broker.get_contract_catalog_status()

    broker._handle_contract_catalog_response(_row("a2701"), {}, 11, True)

    assert [item.symbol for item in broker.get_contract_catalog()] == ["a2612"]
    assert broker.get_contract_catalog_status()["generation"] == original["generation"]
    assert broker.get_contract_catalog_status()["digest"] == original["digest"]
    assert broker.contract_catalog_verified_for_day("20260825") is False
    assert "not registered" in str(broker.get_contract_catalog_status()["sticky_error"])
    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]


def test_ctp_catalog_old_generation_tail_cannot_bind_to_new_trading_day():
    from afuture.broker.ctp import CtpBroker, CtpCredentials

    broker = CtpBroker(CtpCredentials("u", "p", "b", "td", "md", "", ""))
    broker._trading_day = "20260825"
    broker._handle_contract_catalog_response(_row("a2612"), {}, 11, True)
    broker.poll_events()
    broker._trading_day = "20260826"

    broker._handle_contract_catalog_response(_row("a2701"), {}, 11, True)

    assert broker.contract_catalog_verified_for_day("20260826") is False
    assert [item.symbol for item in broker.get_contract_catalog()] == ["a2612"]
    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "not registered" in str(events[0].payload)


def test_ctp_catalog_refresh_uses_official_request_boundary_and_day_cache():
    from types import SimpleNamespace

    from afuture.broker.ctp import CtpBroker, CtpCredentials

    broker = CtpBroker(CtpCredentials("u", "p", "b", "td", "md", "", ""))
    current_day = ["20260825"]
    calls: list[tuple[dict, int]] = []
    td_api = SimpleNamespace(reqid=20, getTradingDay=lambda: current_day[0])

    def query(request, request_id):
        calls.append((request, request_id))
        symbol = "a2612" if current_day[0] == "20260825" else "a2701"
        broker._handle_contract_catalog_response(_row(symbol), {}, request_id, True)
        return 0

    td_api.reqQryInstrument = query
    gateway = SimpleNamespace(td_api=td_api)
    broker._main_engine = SimpleNamespace(get_gateway=lambda _name: gateway)
    broker.is_ready = lambda: True

    first = broker.refresh_contract_catalog(timeout_seconds=0.5)
    cached = broker.refresh_contract_catalog(timeout_seconds=0.5)
    current_day[0] = "20260826"
    second = broker.refresh_contract_catalog(timeout_seconds=0.5)

    assert first.trading_day == "20260825"
    assert cached == first
    assert second.trading_day == "20260826"
    assert second.generation == 2
    assert calls == [({}, 21), ({}, 22)]
    assert [item.symbol for item in broker.get_contract_catalog()] == ["a2701"]


def test_ctp_catalog_refresh_failure_keeps_prior_but_rejects_new_day():
    from types import SimpleNamespace

    from afuture.broker.ctp import CtpBroker, CtpCredentials

    broker = CtpBroker(CtpCredentials("u", "p", "b", "td", "md", "", ""))
    current_day = ["20260825"]
    td_api = SimpleNamespace(reqid=0, getTradingDay=lambda: current_day[0])

    def query(_request, request_id):
        if current_day[0] == "20260825":
            broker._handle_contract_catalog_response(_row("a2612"), {}, request_id, True)
        else:
            broker._handle_contract_catalog_response(
                None,
                {"ErrorID": 7, "ErrorMsg": "not ready"},
                request_id,
                True,
            )
        return 0

    td_api.reqQryInstrument = query
    gateway = SimpleNamespace(td_api=td_api)
    broker._main_engine = SimpleNamespace(get_gateway=lambda _name: gateway)
    broker.is_ready = lambda: True
    verified = broker.refresh_contract_catalog(timeout_seconds=0.5)
    current_day[0] = "20260826"

    with pytest.raises(RuntimeError, match="catalog query failed"):
        broker.refresh_contract_catalog(timeout_seconds=0.5)

    assert broker.get_contract_catalog_status()["digest"] == verified.catalog_digest
    assert broker.contract_catalog_verified_for_day("20260826") is False
