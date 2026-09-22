"""Inject native query callbacks into production CtpBroker; no field acceptance claim."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from afuture.broker.ctp import CtpBroker, CtpCredentials
from afuture.broker.ctp_settlement_query import CtpSettlementQuery, CtpSettlementQueryError
from afuture.models import AccountSnapshot, Offset, OrderRequest, OrderSide


def fragment(sequence=0, content="synthetic settlement\n", **changes):
    return {
        "BrokerID": "9999",
        "InvestorID": "investor",
        "AccountID": "account",
        "CurrencyID": "CNY",
        "TradingDay": "20260921",
        "SettlementID": 1,
        "SequenceNo": sequence,
        "Content": content,
        **changes,
    }


def broker_with_native_query(callback):
    broker = CtpBroker(
        CtpCredentials(
            "user",
            "unused",
            "9999",
            "tcp://test.invalid:1",
            "tcp://test.invalid:2",
            "app",
            "unused",
            environment="test",
            account_id="account",
            currency_id="CNY",
            investor_id="investor",
        )
    )
    api = SimpleNamespace(
        getTradingDay=lambda: "20260922",
        reqid=100,
        login_status=True,
        contract_inited=True,
        reqQrySettlementInfo=lambda request, reqid: callback(broker, request, reqid),
    )
    gateway = SimpleNamespace(td_api=api, md_api=SimpleNamespace(login_status=True))
    broker._main_engine = SimpleNamespace(get_gateway=lambda _: gateway)
    broker._account_identity_verified_state = True
    broker._account_identity_verified_day = "20260922"
    broker._last_account = AccountSnapshot(
        balance=100,
        equity=100,
        available=100,
        margin=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trading_day="20260922",
        cash_flow_verified=True,
    )
    return broker


def deliver(broker, request, reqid):
    assert request == {
        "BrokerID": "9999",
        "InvestorID": "investor",
        "AccountID": "account",
        "CurrencyID": "CNY",
        "TradingDay": "20260921",
    }
    broker._settlement_query.observe(fragment(1, "second\n"), {}, reqid, False)
    broker._settlement_query.observe(fragment(0, "first\n"), {}, reqid, False)
    broker._settlement_query.observe(fragment(0, "first\n"), {}, reqid, False)
    broker._settlement_query.observe(None, {}, reqid, True)
    return 0


def test_production_broker_captures_identity_bound_ordered_fragments_without_financial_authority():
    broker = broker_with_native_query(deliver)
    account_before = broker._last_account
    document = broker.capture_settlement_document("20260921", timeout_seconds=0.1)
    assert document.request_id == 101
    assert document.content == "first\nsecond\n"
    assert len(document.evidence_digest) == 64
    envelope = document.to_dict()
    assert envelope["query_end_received"] is True
    assert envelope["native_bytes_preserved"] is False
    assert envelope["financial_continuity_verified"] is False
    assert envelope["sequence_origin_verified"] is False
    assert broker._last_account == account_before
    assert broker._critical_enqueued == 0


@pytest.mark.parametrize(
    "change",
    [
        {"BrokerID": "other"},
        {"InvestorID": "other"},
        {"AccountID": "other"},
        {"CurrencyID": "USD"},
        {"TradingDay": "20260920"},
        {"Content": ""},
        {"Content": "\ufffd"},
        {"Content": "a\x00b"},
        {"Content": b"bytes"},
        {"SequenceNo": True},
        {"SettlementID": True},
        {"Unknown": 1},
    ],
)
def test_changed_or_uninterpretable_native_identity_is_rejected(change):
    def invalid(broker, request, reqid):
        broker._settlement_query.observe(fragment(**change), {}, reqid, True)
        return 0

    broker = broker_with_native_query(invalid)
    with pytest.raises(CtpSettlementQueryError):
        broker.capture_settlement_document("20260921", timeout_seconds=0.1)
    assert broker._settlement_query.integrity_error


@pytest.mark.parametrize(
    "case", ["gap", "conflict", "mixed_settlement", "wrong_request", "duplicate_end"]
)
def test_fragments_cannot_manufacture_query_completeness(case):
    def invalid(broker, request, reqid):
        sink = broker._settlement_query
        sink.observe(fragment(0, "a"), {}, reqid, False)
        if case == "gap":
            sink.observe(fragment(2, "c"), {}, reqid, True)
        elif case == "conflict":
            sink.observe(fragment(0, "b"), {}, reqid, True)
        elif case == "mixed_settlement":
            sink.observe(fragment(1, "b", SettlementID=2), {}, reqid, True)
        elif case == "wrong_request":
            sink.observe(fragment(1, "b"), {}, reqid + 1, True)
        else:
            sink.observe(None, {}, reqid, True)
            sink.observe(None, {}, reqid, True)
        return 0

    broker = broker_with_native_query(invalid)
    with pytest.raises(CtpSettlementQueryError):
        broker.capture_settlement_document("20260921", timeout_seconds=0.1)


def test_no_end_marker_is_not_a_complete_query_and_late_timeout_is_retired():
    old = []

    def unfinished(broker, request, reqid):
        old.append(reqid)
        broker._settlement_query.observe(fragment(), {}, reqid, False)
        return 0

    broker = broker_with_native_query(unfinished)
    with pytest.raises(CtpSettlementQueryError, match="did not finish"):
        broker.capture_settlement_document("20260921", timeout_seconds=0.002)
    broker._settlement_query.observe(None, {}, old[0], True)
    assert not broker._settlement_query.integrity_error
    broker._main_engine.get_gateway("CTP").td_api.reqQrySettlementInfo = lambda request, reqid: (
        deliver(broker, request, reqid)
    )
    assert (
        broker.capture_settlement_document("20260921", timeout_seconds=0.1).content
        == "first\nsecond\n"
    )


def test_late_reply_after_success_invalidates_capture_without_account_mutation():
    broker = broker_with_native_query(deliver)
    document = broker.capture_settlement_document("20260921", timeout_seconds=0.1)
    broker._settlement_query.observe(fragment(), {}, document.request_id, True)
    with pytest.raises(CtpSettlementQueryError, match="no longer"):
        broker._settlement_query.require_current(document)
    assert broker.health_error() == broker._settlement_query.integrity_error


def test_successful_capture_is_not_reused_for_another_query():
    broker = broker_with_native_query(deliver)
    first = broker.capture_settlement_document("20260921", timeout_seconds=0.1)
    second = broker.capture_settlement_document("20260921", timeout_seconds=0.1)
    assert second.request_id > first.request_id
    with pytest.raises(CtpSettlementQueryError):
        broker._settlement_query.require_current(first)
    broker._settlement_query.require_current(second)


def test_query_refuses_unverified_current_account_before_native_call():
    broker = broker_with_native_query(lambda *_: pytest.fail("native call must not happen"))
    broker._last_account = replace(broker._last_account, cash_flow_verified=False)
    with pytest.raises(RuntimeError, match="verified explicit"):
        broker.capture_settlement_document("20260921")


def test_capture_approval_is_checked_before_reading_configuration(tmp_path):
    from afuture.settlement_capture import capture_test_settlement

    with pytest.raises(RuntimeError, match="explicit isolated test"):
        capture_test_settlement(
            config_path=tmp_path / "absent.toml",
            trading_day="20260921",
            output_path=tmp_path / "private.json",
            confirm_test_connection=False,
        )
    assert list(tmp_path.iterdir()) == []


def test_routed_capture_command_has_no_implicit_counter_authorization(tmp_path, capsys):
    from afuture.command_router import main

    assert (
        main(
            [
                "ctp-settlement-capture",
                "--config",
                str(tmp_path / "absent.toml"),
                "--output",
                str(tmp_path / "private.json"),
                "--trading-day",
                "20260921",
            ]
        )
        == 2
    )
    assert '"orders_sent":0' in capsys.readouterr().out
    assert not (tmp_path / "private.json").exists()


@pytest.mark.parametrize("status", [-1, -9, True, None])
def test_submit_failure_does_not_blindly_resend(status):
    calls = []

    def refused(broker, request, reqid):
        calls.append(reqid)
        return status

    broker = broker_with_native_query(refused)
    with pytest.raises(CtpSettlementQueryError):
        broker.capture_settlement_document("20260921", timeout_seconds=0.1)
    assert len(calls) == 1


def test_query_missing_document_is_distinct_from_protocol_corruption():
    query = CtpSettlementQuery()

    def empty(request, reqid):
        query.observe(None, {}, reqid, True)
        return 0

    with pytest.raises(CtpSettlementQueryError, match="not available"):
        query.query(
            empty,
            request_id=1,
            broker_id="b",
            investor_id="i",
            account_id="a",
            currency_id="CNY",
            trading_day="20260921",
            timeout_seconds=0.1,
        )
    assert not query.integrity_error


@pytest.mark.parametrize("failure", ["snapshot", "settlement"])
def test_query_integrity_failure_blocks_native_send_before_error_event_is_consumed(failure):
    broker = broker_with_native_query(deliver)
    broker._main_engine.send_order = lambda *_args: pytest.fail("native send must not happen")
    if failure == "snapshot":
        broker._snapshot_query_error = "account query is corrupt"
    else:
        # An unsolicited native callback makes the real capture component sticky.
        broker._settlement_query.observe(fragment(), {}, 1, True)
    assert broker.is_ready()
    request = OrderRequest("rb2610", "SHFE", OrderSide.BUY, Offset.OPEN, 1, 3500.0)
    with pytest.raises(RuntimeError, match="query evidence is invalid"):
        broker.send_order(request)
