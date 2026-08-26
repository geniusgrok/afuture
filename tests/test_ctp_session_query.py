from __future__ import annotations

import json
from pathlib import Path

import pytest


def _identity() -> dict[str, str]:
    return {
        "broker_id": "9999",
        "investor_id": "investor-01",
        "invest_unit_id": "unit-01",
        "trading_day": "20260825",
    }


def _order(**overrides) -> dict[str, object]:
    row: dict[str, object] = {
        "BrokerID": "9999",
        "InvestorID": "investor-01",
        "InvestUnitID": "unit-01",
        "TradingDay": "20260825",
        "InstrumentID": "A2612",
        "ExchangeID": "DCE",
        "OrderSysID": "sys-1",
        "FrontID": 3,
        "SessionID": 4,
        "OrderRef": "17",
        "OrderStatus": "0",
        "Direction": "0",
        "CombOffsetFlag": "0",
        "VolumeTotalOriginal": 1,
        "VolumeTraded": 1,
        "VolumeTotal": 0,
        "LimitPrice": 100.0,
        "OrderPriceType": "2",
        "TimeCondition": "3",
        "VolumeCondition": "1",
    }
    row.update(overrides)
    return row


def _trade(**overrides) -> dict[str, object]:
    row: dict[str, object] = {
        "BrokerID": "9999",
        "InvestorID": "investor-01",
        "InvestUnitID": "unit-01",
        "TradingDay": "20260825",
        "InstrumentID": "A2612",
        "ExchangeID": "DCE",
        "TradeID": "trade-1",
        "OrderSysID": "sys-1",
        "OrderRef": "17",
        "Direction": "0",
        "OffsetFlag": "0",
        "Volume": 1,
        "Price": 100.0,
        "TradeDate": "20260825",
        "TradeTime": "09:01:02",
    }
    row.update(overrides)
    return row


def test_request_bound_empty_query_requires_explicit_last() -> None:
    from afuture.broker.ctp_session_query import CtpSessionQueryAccumulator

    accumulator = CtpSessionQueryAccumulator("order")
    accumulator.begin(11, **_identity())

    assert accumulator.observe(11, None, {}, last=False) is None
    completed = accumulator.observe(11, None, {}, last=True)

    assert completed == ()


def test_ctp_fok_uses_complete_volume_and_rejects_minimum_volume() -> None:
    from afuture.broker.ctp_session_query import (
        CtpSessionQueryIntegrityError,
        normalize_ctp_session_order,
    )
    from afuture.models import OrderType

    fok = normalize_ctp_session_order(
        _order(TimeCondition="1", VolumeCondition="3"),
        **_identity(),
    )

    assert fok.order_type is OrderType.FOK
    with pytest.raises(CtpSessionQueryIntegrityError, match="unsupported"):
        normalize_ctp_session_order(
            _order(TimeCondition="1", VolumeCondition="2"),
            **_identity(),
        )


def test_session_query_rejects_wrong_request_day_account_and_late_rows() -> None:
    from afuture.broker.ctp_session_query import (
        CtpSessionQueryAccumulator,
        CtpSessionQueryIntegrityError,
    )

    accumulator = CtpSessionQueryAccumulator("order")
    accumulator.begin(11, **_identity())
    with pytest.raises(CtpSessionQueryIntegrityError, match="request id"):
        accumulator.observe(12, _order(), {}, last=False)

    for field, value, match in (
        ("TradingDay", "20260826", "trading day"),
        ("InvestorID", "manual-account", "investor"),
    ):
        accumulator = CtpSessionQueryAccumulator("order")
        accumulator.begin(11, **_identity())
        with pytest.raises(CtpSessionQueryIntegrityError, match=match):
            accumulator.observe(11, _order(**{field: value}), {}, last=True)

    accumulator = CtpSessionQueryAccumulator("order")
    accumulator.begin(11, **_identity())
    assert accumulator.observe(11, _order(), {}, last=True) is not None
    with pytest.raises(CtpSessionQueryIntegrityError, match="active request"):
        accumulator.observe(11, _order(OrderSysID="sys-2"), {}, last=True)


def test_session_query_rejects_duplicate_or_error_response() -> None:
    from afuture.broker.ctp_session_query import (
        CtpSessionQueryAccumulator,
        CtpSessionQueryIntegrityError,
    )

    accumulator = CtpSessionQueryAccumulator("trade")
    accumulator.begin(21, **_identity())
    accumulator.observe(21, _trade(), {}, last=False)
    with pytest.raises(CtpSessionQueryIntegrityError, match="duplicate"):
        accumulator.observe(21, _trade(), {}, last=True)

    accumulator = CtpSessionQueryAccumulator("trade")
    accumulator.begin(21, **_identity())
    with pytest.raises(CtpSessionQueryIntegrityError, match="ErrorID=7"):
        accumulator.observe(21, None, {"ErrorID": 7, "ErrorMsg": "denied"}, last=True)


def test_complete_session_evidence_round_trips_and_never_falls_back_to_prev(
    tmp_path: Path,
) -> None:
    from afuture.broker.ctp_session_query import (
        CtpSessionActivityEvidenceStore,
        CtpSessionQueryIntegrityError,
        build_ctp_session_activity_evidence,
        normalize_ctp_session_order,
        normalize_ctp_session_trade,
    )

    identity = _identity()
    evidence = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=21,
        orders=(normalize_ctp_session_order(_order(), **identity),),
        trades=(normalize_ctp_session_trade(_trade(), **identity),),
        critical_generation=5,
    )
    store = CtpSessionActivityEvidenceStore(tmp_path / "session.json")
    first = store.save(evidence)
    second = store.save(evidence)

    assert first.sequence == 1
    assert second.sequence == 2
    assert store.load_required_record() == second
    assert store.previous_path.exists()
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["evidence"]["critical_generation"] = 6
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CtpSessionQueryIntegrityError, match="checksum"):
        store.load_required_record()
    assert store.load_previous_record() == first


def test_complete_session_evidence_rejects_invalid_or_duplicate_typed_rows() -> None:
    from dataclasses import replace

    from afuture.broker.ctp_session_query import (
        CtpSessionQueryIntegrityError,
        build_ctp_session_activity_evidence,
        normalize_ctp_session_order,
    )

    identity = _identity()
    order = normalize_ctp_session_order(_order(), **identity)
    with pytest.raises(CtpSessionQueryIntegrityError, match="FrontID"):
        build_ctp_session_activity_evidence(
            account_identity_digest="a" * 64,
            trading_day="20260825",
            order_request_id=11,
            trade_request_id=21,
            orders=(replace(order, front_id=-1),),
            trades=(),
            critical_generation=0,
        )
    with pytest.raises(CtpSessionQueryIntegrityError, match="duplicate"):
        build_ctp_session_activity_evidence(
            account_identity_digest="a" * 64,
            trading_day="20260825",
            order_request_id=11,
            trade_request_id=21,
            orders=(order, order),
            trades=(),
            critical_generation=0,
        )


def test_complete_session_store_rejects_duplicate_json_keys_and_broken_prev_chain(
    tmp_path: Path,
) -> None:
    from afuture.broker.ctp_session_query import (
        CtpSessionActivityEvidenceStore,
        CtpSessionQueryIntegrityError,
        build_ctp_session_activity_evidence,
    )

    evidence = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=21,
        orders=(),
        trades=(),
        critical_generation=0,
    )
    duplicate_store = CtpSessionActivityEvidenceStore(tmp_path / "duplicate.json")
    duplicate_store.save(evidence)
    payload = duplicate_store.path.read_text(encoding="utf-8")
    duplicate_store.path.write_text(
        payload.replace('"kind":', '"kind":"forged","kind":', 1),
        encoding="utf-8",
    )
    with pytest.raises(CtpSessionQueryIntegrityError, match="duplicate"):
        duplicate_store.load_required_record()

    chain_store = CtpSessionActivityEvidenceStore(tmp_path / "chain.json")
    chain_store.save(evidence)
    chain_store.save(evidence)
    chain_store.previous_path.unlink()
    with pytest.raises(CtpSessionQueryIntegrityError, match="previous"):
        chain_store.load_required_record()


def test_session_ownership_joins_trade_via_exchange_order_sys_to_durable_order() -> None:
    from types import SimpleNamespace

    from afuture.broker.ctp_session_query import (
        build_ctp_session_activity_evidence,
        normalize_ctp_session_order,
        normalize_ctp_session_trade,
        validate_ctp_session_activity_ownership,
    )
    from afuture.models import Offset, OrderRequest, OrderSide, OrderType

    identity = _identity()
    order = normalize_ctp_session_order(_order(), **identity)
    trade = normalize_ctp_session_trade(_trade(), **identity)
    evidence = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=21,
        orders=(order,),
        trades=(trade,),
        critical_generation=5,
    )
    entry = SimpleNamespace(
        account_identity_digest="a" * 64,
        target_trading_day="20260825",
        front_id=3,
        session_id=4,
        order_ref=17,
        order_id="CTP.3_4_17",
        request=OrderRequest(
            "A2612",
            "DCE",
            OrderSide.BUY,
            Offset.OPEN,
            1,
            100,
            OrderType.LIMIT,
            "stress90:test",
        ),
        status="terminal",
        fill_keys=("20260825:DCE:CTP.trade-1",),
    )

    digest = validate_ctp_session_activity_ownership(evidence, (entry,))

    assert len(digest) == 64
    with pytest.raises(ValueError, match="unknown CTP session order"):
        validate_ctp_session_activity_ownership(evidence, ())
    mismatched = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=21,
        orders=(order,),
        trades=(normalize_ctp_session_trade(_trade(OrderSysID="manual-sys"), **identity),),
        critical_generation=5,
    )
    with pytest.raises(ValueError, match="OrderSysID"):
        validate_ctp_session_activity_ownership(mismatched, (entry,))


def test_session_ownership_rejects_active_status_and_requires_bidirectional_fills() -> None:
    from types import SimpleNamespace

    from afuture.broker.ctp_session_query import (
        build_ctp_session_activity_evidence,
        normalize_ctp_session_order,
        normalize_ctp_session_trade,
        validate_ctp_session_activity_ownership,
    )
    from afuture.models import Offset, OrderRequest, OrderSide, OrderType

    identity = _identity()
    request = OrderRequest(
        "A2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100,
        OrderType.LIMIT,
        "stress90:test",
    )
    base_entry = SimpleNamespace(
        account_identity_digest="a" * 64,
        target_trading_day="20260825",
        front_id=3,
        session_id=4,
        order_ref=17,
        order_id="CTP.3_4_17",
        request=request,
        status="terminal",
        fill_keys=("20260825:DCE:CTP.trade-1",),
    )
    active = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=21,
        orders=(
            normalize_ctp_session_order(
                _order(OrderStatus="3", VolumeTraded=0, VolumeTotal=1),
                **identity,
            ),
        ),
        trades=(),
        critical_generation=0,
    )
    with pytest.raises(ValueError, match="active CTP session order"):
        validate_ctp_session_activity_ownership(active, (base_entry,))

    missing_trade = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=21,
        orders=(normalize_ctp_session_order(_order(OrderStatus="0"), **identity),),
        trades=(),
        critical_generation=0,
    )
    with pytest.raises(ValueError, match="query trade volume mismatch"):
        validate_ctp_session_activity_ownership(missing_trade, (base_entry,))

    complete = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=21,
        orders=(normalize_ctp_session_order(_order(OrderStatus="0"), **identity),),
        trades=(normalize_ctp_session_trade(_trade(), **identity),),
        critical_generation=0,
    )
    submitted = SimpleNamespace(**{**vars(base_entry), "status": "submitted"})
    with pytest.raises(ValueError, match="status mismatch"):
        validate_ctp_session_activity_ownership(complete, (submitted,))


def test_session_order_rejects_unknown_ctp_status() -> None:
    from afuture.broker.ctp_session_query import (
        CtpSessionQueryIntegrityError,
        normalize_ctp_session_order,
    )

    with pytest.raises(CtpSessionQueryIntegrityError, match="status"):
        normalize_ctp_session_order(_order(OrderStatus="a"), **_identity())


def test_session_recovery_plan_rolls_submitted_terminal_fill_forward() -> None:
    from types import SimpleNamespace

    from afuture.broker.ctp_session_query import (
        build_ctp_session_activity_evidence,
        normalize_ctp_session_order,
        normalize_ctp_session_trade,
        plan_ctp_session_journal_recovery,
    )
    from afuture.models import Offset, OrderRequest, OrderSide, OrderType

    request = OrderRequest(
        "A2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100.0,
        OrderType.LIMIT,
        "stress90:test",
    )
    entry = SimpleNamespace(
        account_identity_digest="a" * 64,
        target_trading_day="20260825",
        front_id=3,
        session_id=4,
        order_ref=17,
        order_id="CTP.3_4_17",
        request=request,
        status="submitted",
        fill_keys=(),
        fill_evidence=(),
    )
    identity = _identity()
    evidence = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=12,
        orders=(normalize_ctp_session_order(_order(OrderStatus="0"), **identity),),
        trades=(normalize_ctp_session_trade(_trade(), **identity),),
        critical_generation=0,
    )

    plan = plan_ctp_session_journal_recovery(evidence, (entry,))

    assert plan.status_updates == (("CTP.3_4_17", "terminal"),)
    assert plan.active_order_ids == ()
    assert plan.fill_trades[0][0] == "CTP.3_4_17"
    assert plan.fill_trades[0][1][0].trade_id == "CTP.trade-1"
    assert tuple(trade.trade_id for trade in plan.session_trades) == ("CTP.trade-1",)


@pytest.mark.parametrize(
    ("current_order_ids", "expected_status_updates", "expected_session_trades"),
    [
        (None, (("CTP.3_4_17", "terminal"),), ("CTP.trade-1",)),
        (frozenset(), (), ()),
    ],
    ids=("current-epoch", "sealed-epoch"),
)
def test_session_recovery_plan_only_republishes_current_epoch_fill(
    current_order_ids: frozenset[str] | None,
    expected_status_updates: tuple[tuple[str, str], ...],
    expected_session_trades: tuple[str, ...],
) -> None:
    from types import SimpleNamespace

    from afuture.broker.ctp_order_journal import ctp_order_fill_evidence
    from afuture.broker.ctp_session_query import (
        build_ctp_session_activity_evidence,
        normalize_ctp_session_order,
        normalize_ctp_session_trade,
        plan_ctp_session_journal_recovery,
    )
    from afuture.models import Offset, OrderRequest, OrderSide, OrderType

    identity = _identity()
    trade = normalize_ctp_session_trade(_trade(), **identity)
    request = OrderRequest(
        "A2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100.0,
        OrderType.LIMIT,
        "stress90:test",
    )
    fill = ctp_order_fill_evidence(
        "20260825",
        trade.to_domain_trade("CTP.3_4_17"),
    )
    entry = SimpleNamespace(
        account_identity_digest="a" * 64,
        target_trading_day="20260825",
        front_id=3,
        session_id=4,
        order_ref=17,
        order_id="CTP.3_4_17",
        request=request,
        status="terminal",
        filled_volume=1,
        fill_keys=(fill.key,),
        fill_evidence=(fill,),
    )
    evidence = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=12,
        orders=(normalize_ctp_session_order(_order(), **identity),),
        trades=(trade,),
        critical_generation=0,
    )

    kwargs = {} if current_order_ids is None else {"current_order_ids": current_order_ids}
    plan = plan_ctp_session_journal_recovery(evidence, (entry,), **kwargs)

    assert plan.fill_trades == ()
    assert plan.status_updates == expected_status_updates
    assert tuple(item.trade_id for item in plan.session_trades) == expected_session_trades


@pytest.mark.parametrize(
    ("status", "traded", "remaining"),
    [("0", 1, 0), ("1", 1, 1), ("2", 1, 1)],
)
def test_session_recovery_rejects_fillless_all_or_partial_traded_order(
    status: str,
    traded: int,
    remaining: int,
) -> None:
    from types import SimpleNamespace

    from afuture.broker.ctp_session_query import (
        build_ctp_session_activity_evidence,
        normalize_ctp_session_order,
        plan_ctp_session_journal_recovery,
    )
    from afuture.models import Offset, OrderRequest, OrderSide, OrderType

    original = traded + remaining
    request = OrderRequest(
        "A2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        original,
        100.0,
        OrderType.LIMIT,
        "stress90:test",
    )
    entry = SimpleNamespace(
        account_identity_digest="a" * 64,
        target_trading_day="20260825",
        front_id=3,
        session_id=4,
        order_ref=17,
        order_id="CTP.3_4_17",
        request=request,
        status="submitted",
        fill_keys=(),
        fill_evidence=(),
    )
    evidence = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=12,
        orders=(
            normalize_ctp_session_order(
                _order(
                    OrderStatus=status,
                    VolumeTotalOriginal=original,
                    VolumeTraded=traded,
                    VolumeTotal=remaining,
                ),
                **_identity(),
            ),
        ),
        trades=(),
        critical_generation=0,
    )

    with pytest.raises(ValueError, match="trade volume"):
        plan_ctp_session_journal_recovery(evidence, (entry,))


def test_session_recovery_plan_waits_owned_active_and_aborts_unsent_prepare() -> None:
    from types import SimpleNamespace

    from afuture.broker.ctp_session_query import (
        build_ctp_session_activity_evidence,
        normalize_ctp_session_order,
        plan_ctp_session_journal_recovery,
    )
    from afuture.models import Offset, OrderRequest, OrderSide, OrderType

    request = OrderRequest(
        "A2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100.0,
        OrderType.LIMIT,
        "stress90:test",
    )

    def entry(order_ref: int, status: str):
        return SimpleNamespace(
            account_identity_digest="a" * 64,
            target_trading_day="20260825",
            front_id=3,
            session_id=4,
            order_ref=order_ref,
            order_id=f"CTP.3_4_{order_ref}",
            request=request,
            status=status,
            fill_keys=(),
            fill_evidence=(),
        )

    identity = _identity()
    evidence = build_ctp_session_activity_evidence(
        account_identity_digest="a" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=12,
        orders=(
            normalize_ctp_session_order(
                _order(OrderStatus="3", VolumeTraded=0, VolumeTotal=1),
                **identity,
            ),
        ),
        trades=(),
        critical_generation=0,
    )

    plan = plan_ctp_session_journal_recovery(
        evidence,
        (entry(17, "prepared"), entry(18, "prepared")),
    )

    assert plan.status_updates == (
        ("CTP.3_4_17", "submitted"),
        ("CTP.3_4_18", "aborted_before_send"),
    )
    assert plan.active_order_ids == ("CTP.3_4_17",)
