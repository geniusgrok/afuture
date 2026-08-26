from datetime import datetime, timezone
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from afuture.broker.ctp import CtpBroker, CtpCredentials, build_ctp_setting
from afuture.models import (
    ContractInfo,
    ContractPosition,
    Offset,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)


def credentials(
    *,
    user_id: str = "user",
    account_id: str = "",
    currency_id: str = "",
    investor_id: str = "",
    invest_unit_id: str = "",
):
    return CtpCredentials(
        user_id,
        "secret",
        "9999",
        "tcp://td",
        "tcp://md",
        "app",
        "auth",
        "test",
        account_id=account_id,
        currency_id=currency_id,
        investor_id=investor_id,
        invest_unit_id=invest_unit_id,
    )


def authoritative_credentials():
    return credentials(
        account_id="acct-01",
        currency_id="CNY",
        investor_id="investor-01",
        invest_unit_id="unit-01",
    )


def raw_account_evidence(
    *,
    trading_day: str = "20260821",
    settlement_id: int = 42,
    pre_balance: float = 498_000.0,
    deposit: float = 10_000.0,
    withdrawal: float = 2_000.0,
) -> dict[str, object]:
    return {
        "BrokerID": "9999",
        "AccountID": "acct-01",
        "CurrencyID": "CNY",
        "InvestorID": "investor-01",
        "InvestUnitID": "unit-01",
        "TradingDay": trading_day,
        "Deposit": deposit,
        "Withdraw": withdrawal,
        "PreBalance": pre_balance,
        "SettlementID": settlement_id,
    }


def account_data(*, account_id: str = "acct-01", balance: float = 508_000.0):
    return SimpleNamespace(accountid=account_id, balance=balance, available=balance)


def raw_tick(
    symbol: str = "cu2609",
    exchange: str = "SHFE",
    *,
    price: float = 70_000.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        exchange=SimpleNamespace(value=exchange),
        datetime=datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc),
        bid_price_1=price - 1,
        ask_price_1=price + 1,
        last_price=price,
        bid_volume_1=10,
        ask_volume_1=10,
        limit_up=80_000,
        limit_down=60_000,
        volume=100,
        open_interest=200,
    )


def test_lifecycle_state_commit_fence_linearizes_critical_callback_ingress():
    broker = CtpBroker(authoritative_credentials())
    entered = Event()
    completed = Event()

    def enter_callback() -> None:
        entered.set()
        broker._mark_critical_upstream_pending("account")
        completed.set()

    with broker.lifecycle_state_commit_fence():
        worker = Thread(target=enter_callback)
        worker.start()
        assert entered.wait(1.0)
        assert completed.wait(0.05) is False
    assert completed.wait(1.0)
    worker.join(timeout=1.0)


def test_ctp_setting_matches_gateway_contract():
    setting = build_ctp_setting(credentials())
    assert (
        setting["用户名"] == "user"
        and setting["经纪商代码"] == "9999"
        and setting["柜台环境"] == "测试"
    )


def test_ctp_refresh_session_activity_requires_request_bound_order_and_trade_last() -> None:
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260825"
    query_order: list[str] = []

    class TdApi:
        reqid = 40

        def getTradingDay(self):
            return "20260825"

        def reqQryOrder(self, request, request_id):
            query_order.append("order")
            assert request == {
                "BrokerID": "9999",
                "InvestorID": "investor-01",
                "InvestUnitID": "unit-01",
            }
            broker._handle_session_query_response(
                "order",
                {
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
                },
                {},
                request_id,
                True,
            )
            return 0

        def reqQryTrade(self, request, request_id):
            query_order.append("trade")
            assert request["InvestorID"] == "investor-01"
            broker._handle_session_query_response(
                "trade",
                {
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
                },
                {},
                request_id,
                True,
            )
            return 0

    td_api = TdApi()
    gateway = SimpleNamespace(td_api=td_api)
    broker._main_engine = SimpleNamespace(get_gateway=lambda _name: gateway)

    evidence = broker.refresh_session_activity(timeout_seconds=0.1)

    assert evidence.trading_day == "20260825"
    assert query_order == ["trade", "order"]
    assert evidence.trade_request_id == 41
    assert evidence.order_request_id == 42
    assert evidence.orders[0].order_id == "CTP.3_4_17"
    assert evidence.trades[0].identity == "DCE:trade-1"
    assert td_api.reqid == 42


def test_ctp_session_query_captures_remote_order_created_between_query_kinds() -> None:
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260825"
    remote_order_exists = False

    class TdApi:
        reqid = 70

        def getTradingDay(self):
            return "20260825"

        def reqQryTrade(self, _request, request_id):
            nonlocal remote_order_exists
            broker._handle_session_query_response("trade", None, {}, request_id, True)
            remote_order_exists = True
            return 0

        def reqQryOrder(self, _request, request_id):
            row = None
            if remote_order_exists:
                row = {
                    "BrokerID": "9999",
                    "InvestorID": "investor-01",
                    "InvestUnitID": "unit-01",
                    "TradingDay": "20260825",
                    "InstrumentID": "A2612",
                    "ExchangeID": "DCE",
                    "OrderSysID": "remote-active",
                    "FrontID": 3,
                    "SessionID": 4,
                    "OrderRef": "91",
                    "OrderStatus": "3",
                    "Direction": "0",
                    "CombOffsetFlag": "0",
                    "VolumeTotalOriginal": 1,
                    "VolumeTraded": 0,
                    "VolumeTotal": 1,
                    "LimitPrice": 100.0,
                    "OrderPriceType": "2",
                    "TimeCondition": "3",
                    "VolumeCondition": "1",
                }
            broker._handle_session_query_response("order", row, {}, request_id, True)
            return 0

    broker._main_engine = SimpleNamespace(
        get_gateway=lambda _name: SimpleNamespace(td_api=TdApi())
    )

    evidence = broker.refresh_session_activity(timeout_seconds=0.1)

    assert [order.order_id for order in evidence.orders] == ["CTP.3_4_91"]
    assert [order.order_id for order in evidence.orders if order.active] == ["CTP.3_4_91"]


def test_ctp_refresh_session_activity_rejects_normal_order_callback_race() -> None:
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260825"

    class TdApi:
        reqid = 50

        def getTradingDay(self):
            return "20260825"

        def reqQryOrder(self, _request, request_id):
            broker._handle_session_query_response("order", None, {}, request_id, True)
            broker._mark_order_trade_activity()
            return 0

        def reqQryTrade(self, _request, request_id):
            broker._handle_session_query_response("trade", None, {}, request_id, True)
            return 0

    broker._main_engine = SimpleNamespace(
        get_gateway=lambda _name: SimpleNamespace(td_api=TdApi())
    )

    with pytest.raises(RuntimeError, match="changed during complete query"):
        broker.refresh_session_activity(timeout_seconds=0.1)


def test_ctp_refresh_session_activity_rejects_queued_order_ingress() -> None:
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260825"
    broker._mark_critical_upstream_pending("order")
    broker._main_engine = SimpleNamespace(
        get_gateway=lambda _name: SimpleNamespace(
            td_api=SimpleNamespace(getTradingDay=lambda: "20260825")
        )
    )

    with pytest.raises(RuntimeError, match="order/trade callbacks are not quiescent"):
        broker.refresh_session_activity(timeout_seconds=0.1)


def test_ctp_refresh_session_activity_rejects_late_row_after_last() -> None:
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260825"

    class TdApi:
        reqid = 60

        def getTradingDay(self):
            return "20260825"

        def reqQryOrder(self, _request, request_id):
            broker._handle_session_query_response("order", None, {}, request_id, True)
            broker._handle_session_query_response(
                "order",
                {
                    "BrokerID": "9999",
                    "InvestorID": "investor-01",
                    "InvestUnitID": "unit-01",
                    "TradingDay": "20260825",
                    "InstrumentID": "A2612",
                    "ExchangeID": "DCE",
                    "OrderSysID": "late",
                    "FrontID": 3,
                    "SessionID": 4,
                    "OrderRef": "18",
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
                },
                {},
                request_id,
                True,
            )
            return 0

        def reqQryTrade(self, _request, request_id):
            broker._handle_session_query_response("trade", None, {}, request_id, True)
            return 0

    broker._main_engine = SimpleNamespace(
        get_gateway=lambda _name: SimpleNamespace(td_api=TdApi())
    )

    with pytest.raises(RuntimeError, match="late response after completion"):
        broker.refresh_session_activity(timeout_seconds=0.1)


def test_ctp_stress90_startup_capability_is_process_local_and_ingress_bound(
    tmp_path,
) -> None:
    from afuture.broker.ctp_session_query import build_ctp_session_activity_evidence

    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260825"
    broker.configure_order_submission_journal(
        tmp_path / "stress90_ctp_orders.json",
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    broker.require_stress90_session_startup_capability()
    broker._main_engine = SimpleNamespace(
        get_gateway=lambda _name: SimpleNamespace(
            td_api=SimpleNamespace(getTradingDay=lambda: "20260825")
        )
    )
    with pytest.raises(RuntimeError, match="startup session capability is missing"):
        broker.require_stress90_session_startup_capability_current()

    evidence = build_ctp_session_activity_evidence(
        account_identity_digest=broker.get_account_identity_digest(),
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=12,
        orders=(),
        trades=(),
        critical_generation=0,
    )
    broker.install_stress90_session_startup_capability(
        evidence=evidence,
        ownership_digest="c" * 64,
    )
    broker.require_stress90_session_startup_capability_current()
    broker._mark_critical_upstream_pending("order")
    with pytest.raises(RuntimeError, match="no longer current"):
        broker.require_stress90_session_startup_capability_current()


def test_ctp_session_evidence_fails_while_late_query_callback_is_at_ingress(
    monkeypatch,
) -> None:
    from threading import Event, Thread

    from afuture.broker.ctp_session_query import build_ctp_session_activity_evidence

    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260825"
    broker._main_engine = SimpleNamespace(
        get_gateway=lambda _name: SimpleNamespace(
            td_api=SimpleNamespace(getTradingDay=lambda: "20260825")
        )
    )
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
    worker.start()
    assert entered.wait(1.0)
    try:
        with pytest.raises(RuntimeError, match="no longer current"):
            broker.require_session_activity_evidence_current(evidence)
    finally:
        release.set()
        worker.join(timeout=1.0)
    assert not worker.is_alive()


def test_ctp_maps_internal_order_with_fake_runtime():
    broker = CtpBroker(credentials())

    class EnumValue:
        def __init__(self, value):
            self.value = value

    class Exchange:
        DCE = EnumValue("DCE")

    class Direction:
        LONG = "LONG"
        SHORT = "SHORT"

    class VOffset:
        OPEN = "OPEN"
        CLOSE = "CLOSE"
        CLOSETODAY = "CLOSETODAY"
        CLOSEYESTERDAY = "CLOSEYESTERDAY"

    class VType:
        LIMIT = "LIMIT"
        FAK = "FAK"
        FOK = "FOK"

    class Req:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    broker._runtime = {
        "Exchange": Exchange,
        "Direction": Direction,
        "Offset": VOffset,
        "OrderType": VType,
        "OrderRequest": Req,
    }
    req = broker._to_vnpy_order(
        OrderRequest("m2609", "DCE", OrderSide.SELL, Offset.CLOSE, 2, 3000, OrderType.FAK, "pair")
    )
    assert (req.symbol, req.direction, req.offset, req.type) == ("m2609", "SHORT", "CLOSE", "FAK")


def test_ctp_account_margin_proxy_and_position_snapshot():
    broker = CtpBroker(credentials())
    broker._trading_day = "20260821"
    account = broker._convert_account(
        SimpleNamespace(balance=500000, available=350000, frozen=20000)
    )
    assert account.margin == 150000
    raw = [
        SimpleNamespace(
            symbol="m2609",
            exchange=SimpleNamespace(value="DCE"),
            direction=SimpleNamespace(name="LONG"),
            volume=3,
            yd_volume=1,
            price=3000.0,
        ),
        SimpleNamespace(
            symbol="m2609",
            exchange=SimpleNamespace(value="DCE"),
            direction=SimpleNamespace(name="SHORT"),
            volume=2,
            yd_volume=2,
            price=3010.0,
        ),
    ]
    broker._handle_position_snapshot(raw)
    p = broker.get_positions()[0]
    assert (p.long_today, p.long_yesterday, p.short_today, p.short_yesterday) == (2, 1, 0, 2)
    assert broker.poll_events()[0].event_type == "position_snapshot"


def test_ctp_account_identity_is_non_secret_and_cash_flow_must_be_verified():
    broker = CtpBroker(authoritative_credentials())
    digest = broker.get_account_identity_digest()
    assert len(digest) == 64
    assert all(
        secret not in digest
        for secret in ("user", "9999", "acct-01", "CNY", "investor-01", "unit-01")
    )

    broker._trading_day = "20260821"
    unverified = broker._convert_account(account_data(balance=500_000))
    assert unverified.cash_flow_verified is False
    assert broker.account_identity_verified is False

    assert broker._handle_account_cash_flow(raw_account_evidence()) is True
    assert broker.account_identity_verified is False
    broker._on_account(SimpleNamespace(data=account_data()))
    verified = broker.get_account()
    assert verified.cash_flow_verified is True
    assert verified.deposit == 10_000.0
    assert verified.withdrawal == 2_000.0
    assert verified.settlement_verified is True
    assert verified.previous_settlement_equity == 498_000.0
    assert verified.settlement_id == 42
    assert broker.account_identity_verified is True


def test_ctp_expected_account_identity_changes_prestart_digest():
    base = CtpBroker(authoritative_credentials()).get_account_identity_digest()

    for field, value in (
        ("account_id", "acct-02"),
        ("currency_id", "USD"),
        ("investor_id", "investor-02"),
        ("invest_unit_id", "unit-02"),
    ):
        kwargs = {
            "account_id": "acct-01",
            "currency_id": "CNY",
            "investor_id": "investor-01",
            "invest_unit_id": "unit-01",
        }
        kwargs[field] = value
        assert CtpBroker(credentials(**kwargs)).get_account_identity_digest() != base


def test_ctp_economic_account_identity_does_not_partition_by_login_user() -> None:
    first = CtpBroker(authoritative_credentials()).get_account_identity_digest()
    second = CtpBroker(
        credentials(
            user_id="alternate-login",
            account_id="acct-01",
            currency_id="CNY",
            investor_id="investor-01",
            invest_unit_id="unit-01",
        )
    ).get_account_identity_digest()

    assert first == second


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("BrokerID", "0000"),
        ("AccountID", "acct-02"),
        ("CurrencyID", "USD"),
        ("InvestorID", "investor-02"),
        ("InvestUnitID", "unit-02"),
    ],
)
def test_ctp_rejects_mismatched_raw_account_identity(field: str, wrong_value: str):
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260821"
    row = raw_account_evidence()
    row[field] = wrong_value

    assert broker._handle_account_cash_flow(row) is False

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert broker.account_identity_verified is False
    assert broker._convert_account(account_data()).cash_flow_verified is False


@pytest.mark.parametrize("field", ["InvestorID", "InvestUnitID"])
@pytest.mark.parametrize("missing_value", [None, ""], ids=["absent", "empty"])
def test_ctp_rejects_missing_configured_raw_investor_identity(
    field: str,
    missing_value: str | None,
):
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260821"
    row = raw_account_evidence()
    if missing_value is None:
        row.pop(field)
    else:
        row[field] = missing_value

    assert broker._handle_account_cash_flow(row) is False

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert field in str(events[0].payload)
    assert broker.account_identity_verified is False


def test_ctp_selects_one_exact_account_data_instead_of_first_row():
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260821"
    assert broker._handle_account_cash_flow(raw_account_evidence()) is True
    broker._on_account(SimpleNamespace(data=account_data(balance=508_000)))
    broker.poll_events()
    broker._main_engine = SimpleNamespace(
        get_all_accounts=lambda: [
            account_data(account_id="other-account", balance=900_000),
            account_data(balance=508_000),
        ],
        get_gateway=lambda _name: SimpleNamespace(
            td_api=SimpleNamespace(getTradingDay=lambda: "20260821")
        ),
    )

    assert broker.get_account().balance == 508_000
    assert broker.account_identity_verified is True

    broker._main_engine.get_all_accounts = lambda: [account_data(), account_data()]
    with pytest.raises(RuntimeError, match="unique exact"):
        broker.get_account()
    assert broker.poll_events()[0].event_type == "broker_error"
    assert broker.account_identity_verified is False


def test_ctp_verified_account_expires_when_authoritative_day_advances():
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260821"
    assert broker._handle_account_cash_flow(raw_account_evidence()) is True
    broker._on_account(SimpleNamespace(data=account_data()))
    broker.poll_events()
    authoritative_day = {"value": "20260821"}
    broker._main_engine = SimpleNamespace(
        get_all_accounts=lambda: [account_data()],
        get_gateway=lambda _name: SimpleNamespace(
            td_api=SimpleNamespace(getTradingDay=lambda: authoritative_day["value"])
        ),
    )
    assert broker.account_identity_verified is True

    authoritative_day["value"] = "20260825"

    assert broker.account_identity_verified is False
    with pytest.raises(RuntimeError, match="verified.*not available"):
        broker.get_account()


def test_ctp_account_data_cannot_reuse_an_old_evidence_generation():
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260821"
    assert broker._handle_account_cash_flow(raw_account_evidence()) is True
    event = SimpleNamespace(data=account_data())
    broker._on_account(event)
    assert broker.account_identity_verified is True
    broker.poll_events()

    broker._on_account(event)

    assert broker.poll_events()[0].event_type == "broker_error"
    assert broker.account_identity_verified is False
    with pytest.raises(RuntimeError, match="not available"):
        broker.get_account()


def test_ctp_fresh_account_evidence_blocks_critical_ack_until_account_data_pair(
    tmp_path,
):
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260821"
    broker.configure_order_submission_journal(
        tmp_path / "stress90_ctp_orders.json",
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )

    assert broker._handle_account_cash_flow(raw_account_evidence()) is True

    assert broker.account_identity_verified is False
    assert broker.acknowledge_critical_events() is False
    assert broker.has_pending_critical_events() is True

    broker._on_account(SimpleNamespace(data=account_data()))
    assert broker.acknowledge_critical_events() is False
    assert [event.event_type for event in broker.poll_events()] == ["account"]
    assert broker.acknowledge_critical_events() is True
    assert broker.account_identity_verified is True


def test_ctp_account_evidence_generations_pair_fifo_before_latest_can_unlock(
    tmp_path,
) -> None:
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260821"
    broker.configure_order_submission_journal(
        tmp_path / "stress90_ctp_orders.json",
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )

    assert broker._handle_account_cash_flow(raw_account_evidence()) is True
    assert broker._handle_account_cash_flow(raw_account_evidence()) is True
    assert [item.generation for item in broker._pending_account_evidence] == [1, 2]

    broker._on_account(SimpleNamespace(data=account_data()))

    first = broker.poll_events()
    assert [event.event_type for event in first] == ["account"]
    assert first[0].payload.cash_flow_verified is False
    assert broker.account_identity_verified is False
    assert broker.acknowledge_critical_events() is False
    assert [item.generation for item in broker._pending_account_evidence] == [2]

    broker._on_account(SimpleNamespace(data=account_data()))
    assert broker.acknowledge_critical_events() is False
    second = broker.poll_events()
    assert [event.event_type for event in second] == ["account"]
    assert second[0].payload.cash_flow_verified is True
    assert broker.acknowledge_critical_events() is True
    assert broker.account_identity_verified is True


def test_ctp_new_account_evidence_generation_invalidates_old_pair(tmp_path):
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260821"
    broker.configure_order_submission_journal(
        tmp_path / "stress90_ctp_orders.json",
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    assert broker._handle_account_cash_flow(raw_account_evidence()) is True
    old_evidence = broker._pending_account_evidence[0]
    broker._on_account(SimpleNamespace(data=account_data()))
    broker.poll_events()
    assert broker.acknowledge_critical_events() is True
    assert broker.account_identity_verified is True

    assert broker._handle_account_cash_flow(raw_account_evidence()) is True

    assert broker._account_evidence_is_current(old_evidence) is False
    assert broker.account_identity_verified is False
    assert broker.acknowledge_critical_events() is False

    broker._on_account(SimpleNamespace(data=account_data()))
    assert [event.event_type for event in broker.poll_events()] == ["account"]
    assert broker.acknowledge_critical_events() is True
    assert broker.account_identity_verified is True


@pytest.mark.parametrize(
    ("field", "mutated"),
    [
        ("Deposit", 10_001.0),
        ("Withdraw", 2_001.0),
        ("PreBalance", 497_999.0),
        ("SettlementID", 43),
    ],
)
def test_ctp_rejects_same_day_settlement_evidence_mutation(field: str, mutated: object):
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260821"
    assert broker._handle_account_cash_flow(raw_account_evidence()) is True
    broker._on_account(SimpleNamespace(data=account_data()))
    broker.poll_events()
    changed = raw_account_evidence()
    changed[field] = mutated

    assert broker._handle_account_cash_flow(changed) is False

    assert broker.poll_events()[0].event_type == "broker_error"
    assert broker.account_identity_verified is False


def test_ctp_treats_settlement_id_as_opaque_across_authoritative_days():
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260821"
    assert broker._handle_account_cash_flow(raw_account_evidence(settlement_id=10)) is True
    broker._on_account(SimpleNamespace(data=account_data()))
    broker.poll_events()

    broker._trading_day = "20260825"
    assert (
        broker._handle_account_cash_flow(
            raw_account_evidence(
                trading_day="20260825",
                settlement_id=13,
                pre_balance=510_000,
                deposit=0,
                withdrawal=0,
            )
        )
        is True
    )
    broker._on_account(SimpleNamespace(data=account_data(balance=512_000)))
    assert broker.get_account().settlement_id == 13
    assert broker.account_identity_verified is True


def test_ctp_rejects_stale_or_backward_account_evidence_day():
    broker = CtpBroker(authoritative_credentials())
    broker._trading_day = "20260825"
    assert (
        broker._handle_account_cash_flow(
            raw_account_evidence(trading_day="20260825", settlement_id=13)
        )
        is True
    )

    assert broker._handle_account_cash_flow(raw_account_evidence()) is False

    assert broker.poll_events()[0].event_type == "broker_error"
    assert broker.account_identity_verified is False

    broker._trading_day = "20260821"
    assert broker._handle_account_cash_flow(raw_account_evidence()) is False
    assert broker.poll_events()[0].event_type == "broker_error"


def test_ctp_legacy_credentials_never_claim_verified_stress_identity():
    broker = CtpBroker(credentials())
    broker._trading_day = "20260821"

    assert broker._handle_account_cash_flow(raw_account_evidence()) is True
    snapshot = broker._convert_account(SimpleNamespace(balance=508_000, available=508_000))

    assert snapshot.cash_flow_verified is False
    assert broker.account_identity_verified is False


def test_ctp_critical_event_precedes_coalesced_tick_flood() -> None:
    broker = CtpBroker(credentials(), max_events_per_poll=2)
    broker._trading_day = "20260825"
    for index in range(500):
        broker._on_tick(SimpleNamespace(data=raw_tick(price=70_000 + index)))
    broker._on_account(SimpleNamespace(data=SimpleNamespace(balance=500_000, available=400_000)))

    events = broker.poll_events()

    assert [event.event_type for event in events] == ["account", "tick"]
    assert events[1].payload.last_price == 70_499


def test_ctp_poll_is_bounded_and_coalesces_latest_tick_per_contract() -> None:
    broker = CtpBroker(credentials(), max_events_per_poll=2)
    broker._trading_day = "20260825"
    broker._on_tick(SimpleNamespace(data=raw_tick("same", "DCE", price=100)))
    broker._on_tick(SimpleNamespace(data=raw_tick("same", "DCE", price=101)))
    broker._on_tick(SimpleNamespace(data=raw_tick("same", "SHFE", price=200)))
    broker._on_tick(SimpleNamespace(data=raw_tick("other", "DCE", price=300)))

    first = broker.poll_events()
    second = broker.poll_events()
    counters = broker.delivery_counters()

    assert len(first) == 2
    assert [
        (event.payload.symbol, event.payload.exchange, event.payload.last_price) for event in first
    ] == [
        ("same", "DCE", 101),
        ("same", "SHFE", 200),
    ]
    assert [(event.payload.symbol, event.payload.exchange) for event in second] == [
        ("other", "DCE")
    ]
    assert counters == {
        "critical_enqueued": 0,
        "ticks_received": 4,
        "ticks_coalesced": 1,
        "critical_delivered": 0,
        "ticks_delivered": 3,
        "critical_backlog": 0,
        "tick_backlog": 0,
    }


def test_ctp_raw_observer_sees_every_tick_before_manager_coalescing() -> None:
    broker = CtpBroker(credentials(), max_events_per_poll=2)
    broker._trading_day = "20260825"
    contract = ContractInfo("A2612", "DCE", "A", "2026-12-15")
    broker._contract_catalog[contract.symbol] = contract
    observed = []

    class Observer:
        def observe_raw_tick(self, tick, metadata):
            assert broker.delivery_counters()["ticks_received"] == len(observed)
            observed.append((tick.last_price, metadata))

    broker.set_raw_tick_observer(Observer())
    for index in range(1_000):
        broker._on_tick(SimpleNamespace(data=raw_tick("A2612", "DCE", price=100.0 + index)))
    broker._on_account(SimpleNamespace(data=SimpleNamespace(balance=500_000, available=400_000)))

    events = broker.poll_events()
    assert len(observed) == 1_000
    assert all(metadata == contract for _price, metadata in observed)
    assert [event.event_type for event in events] == ["account", "tick"]
    assert events[-1].payload.last_price == 1_099.0
    assert broker.delivery_counters()["ticks_coalesced"] == 999


def test_ctp_raw_observer_receives_monotonic_md_connection_generations() -> None:
    broker = CtpBroker(credentials())
    states = []

    class Observer:
        def note_raw_market_connection(self, *, connected, generation):
            states.append((connected, generation))

        def observe_raw_tick(self, tick, metadata):
            del tick, metadata

    broker.set_raw_tick_observer(Observer())
    broker._handle_raw_market_connection_state(True)
    broker._handle_raw_market_connection_state(False)
    broker._handle_raw_market_connection_state(True)

    assert states == [(True, 1), (False, 1), (True, 2)]


def test_ctp_rejects_stale_raw_tick_trading_day_after_rollover() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260826"
    raw = raw_tick("A2612", "DCE", price=100.0)
    raw._afuture_trading_day = "20260825"
    raw._afuture_action_day = "20260825"
    observed = []

    class Observer:
        def observe_raw_tick(self, tick, metadata):
            observed.append((tick, metadata))

    broker.set_raw_tick_observer(Observer())
    broker._on_tick(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert observed == []
    assert [event.event_type for event in events] == ["broker_error"]
    assert "source trading day" in str(events[0].payload)


def test_ctp_raw_observer_failure_is_critical_and_tick_is_not_delivered() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"

    class Observer:
        def observe_raw_tick(self, tick, metadata):
            raise RuntimeError("injected evidence failure")

    broker.set_raw_tick_observer(Observer())
    broker._on_tick(SimpleNamespace(data=raw_tick("A2612", "DCE", price=100.0)))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "injected evidence failure" in str(events[0].payload)
    assert broker.delivery_counters()["ticks_received"] == 0


def test_ctp_typed_nonfatal_evidence_error_keeps_valid_tick_delivery() -> None:
    from afuture.broker.base import RawMarketEvidenceError

    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"

    class Observer:
        def observe_raw_tick(self, tick, metadata):
            del tick, metadata
            raise RawMarketEvidenceError("supported OI coverage is incomplete")

    broker.set_raw_tick_observer(Observer())
    broker._on_tick(SimpleNamespace(data=raw_tick("A2612", "DCE", price=100.0)))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["tick"]
    assert events[0].payload.last_price == 100.0


def test_ctp_fatal_evidence_revision_stops_tick_delivery() -> None:
    from afuture.broker.base import RawMarketEvidenceFatalError

    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"

    class Observer:
        def observe_raw_tick(self, tick, metadata):
            del tick, metadata
            raise RawMarketEvidenceFatalError("completed evidence would change")

    broker.set_raw_tick_observer(Observer())
    broker._on_tick(SimpleNamespace(data=raw_tick("A2612", "DCE", price=100.0)))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "completed evidence would change" in str(events[0].payload)
    assert broker.delivery_counters()["ticks_received"] == 0


@pytest.mark.parametrize("trading_day", ["", "20260230", "2026-08-25"])
def test_ctp_rejects_missing_or_invalid_authoritative_trading_day(trading_day: str) -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = trading_day

    with pytest.raises(RuntimeError, match="trading day"):
        broker.get_trading_day()


def test_ctp_callback_surfaces_missing_trading_day_without_raising() -> None:
    broker = CtpBroker(credentials())

    broker._on_tick(SimpleNamespace(data=raw_tick()))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "trading day" in str(events[0].payload)


@pytest.mark.parametrize(
    "gateway",
    [
        None,
        SimpleNamespace(td_api=None),
        SimpleNamespace(td_api=SimpleNamespace()),
        SimpleNamespace(td_api=SimpleNamespace(getTradingDay=lambda: "")),
    ],
    ids=["missing-gateway", "missing-td-api", "missing-getter", "empty-value"],
)
def test_started_ctp_rejects_missing_current_trading_day_source(gateway: object) -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260824"
    broker._main_engine = SimpleNamespace(get_gateway=lambda _gateway_name: gateway)

    with pytest.raises(RuntimeError, match="trading day"):
        broker.get_trading_day()


@pytest.mark.parametrize("field", ["balance", "available"])
def test_ctp_account_callback_rejects_non_finite_truth_without_mutation(field: str) -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260821"
    broker._last_account = broker._convert_account(
        SimpleNamespace(balance=500000, available=350000)
    )
    before = broker._last_account
    raw = SimpleNamespace(balance=500000, available=350000)
    setattr(raw, field, float("nan"))

    broker._on_account(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["account_error"]
    assert broker._last_account == before
    assert broker._account_event_generation == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("direction", SimpleNamespace(name="NOT_LONG")),
        ("volume", 1.5),
        ("volume", True),
        ("yd_volume", 1.5),
        ("price", float("nan")),
    ],
)
def test_ctp_position_snapshot_rejects_invalid_truth_without_mutation(
    field: str,
    value: object,
) -> None:
    broker = CtpBroker(credentials())
    broker._positions = {"m2609": ContractPosition("m2609", "DCE", long_today=1, long_price=3000.0)}
    before = broker.get_positions()
    raw = SimpleNamespace(
        symbol="m2609",
        exchange=SimpleNamespace(value="DCE"),
        direction=SimpleNamespace(name="LONG"),
        volume=2,
        yd_volume=1,
        price=3000.0,
    )
    setattr(raw, field, value)

    broker._handle_position_snapshot([raw])

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert broker.get_positions() == before
    assert broker._position_snapshot_generation == 0


def test_ctp_snapshot_generation_order_ownership_and_health():
    broker = CtpBroker(credentials(), snapshot_stale_seconds=20)
    broker._last_account = object()
    broker._account_event_generation = 4
    broker._position_snapshot_generation = 7
    marker = broker.snapshot_marker()
    assert not broker.snapshot_ready(marker)
    broker._account_event_generation += 1
    assert not broker.snapshot_ready(marker)
    broker._position_snapshot_generation += 1
    assert broker.snapshot_ready(marker)
    broker._order_references["CTP.1"] = "p"
    assert broker.owns_order("CTP.1") and not broker.owns_order("manual")
    broker._last_account_monotonic = 100
    broker._last_position_snapshot_monotonic = 100
    broker.is_ready = lambda: True
    assert broker.health_error(now_monotonic=110) is None
    assert "stale" in broker.health_error(now_monotonic=121)


def conversion_broker() -> tuple[CtpBroker, SimpleNamespace]:
    broker = CtpBroker(credentials())
    status = SimpleNamespace(
        SUBMITTING=object(),
        NOTTRADED=object(),
        PARTTRADED=object(),
        ALLTRADED=object(),
        CANCELLED=object(),
        REJECTED=object(),
    )
    broker._runtime = {"Status": status}
    return broker, status


def raw_order(status: object) -> SimpleNamespace:
    return SimpleNamespace(
        vt_orderid="CTP.1",
        symbol="cu2609",
        exchange=SimpleNamespace(value="SHFE"),
        direction=SimpleNamespace(name="LONG"),
        offset=SimpleNamespace(name="OPEN"),
        type=SimpleNamespace(name="LIMIT"),
        status=status,
        volume=2,
        traded=0,
        price=70_000.0,
        reference="pair",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("direction", SimpleNamespace(name="UNKNOWN")),
        ("offset", SimpleNamespace(name="FORCECLOSE")),
        ("type", SimpleNamespace(name="MARKET")),
        ("status", object()),
    ],
)
def test_ctp_order_conversion_rejects_unknown_protocol_value(
    field: str,
    value: object,
) -> None:
    broker, status = conversion_broker()
    raw = raw_order(status.NOTTRADED)
    setattr(raw, field, value)

    with pytest.raises(ValueError, match=field):
        broker._convert_order(raw)


@pytest.mark.parametrize(
    ("status_name", "expected"),
    [
        ("SUBMITTING", OrderStatus.SUBMITTING),
        ("NOTTRADED", OrderStatus.NOT_TRADED),
        ("PARTTRADED", OrderStatus.PART_TRADED),
        ("ALLTRADED", OrderStatus.FILLED),
        ("CANCELLED", OrderStatus.CANCELLED),
        ("REJECTED", OrderStatus.REJECTED),
    ],
)
def test_ctp_order_conversion_preserves_supported_status(
    status_name: str,
    expected: OrderStatus,
) -> None:
    broker, status = conversion_broker()

    order = broker._convert_order(raw_order(getattr(status, status_name)))

    assert order.status is expected
    assert order.request.side is OrderSide.BUY
    assert order.request.offset is Offset.OPEN
    assert order.request.order_type is OrderType.LIMIT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("volume", 0),
        ("volume", 1.5),
        ("volume", True),
        ("traded", -1),
        ("traded", 3),
        ("price", float("nan")),
    ],
)
def test_ctp_order_conversion_rejects_invalid_economic_value(
    field: str,
    value: object,
) -> None:
    broker, status = conversion_broker()
    raw = raw_order(status.NOTTRADED)
    setattr(raw, field, value)

    with pytest.raises(ValueError, match=field):
        broker._convert_order(raw)


def raw_trade(
    *,
    direction: str = "LONG",
    offset: str = "OPEN",
    trade_id: str = "CTP.T1",
    symbol: str = "cu2609",
    exchange: str = "SHFE",
) -> SimpleNamespace:
    return SimpleNamespace(
        vt_tradeid=trade_id,
        vt_orderid="CTP.1",
        symbol=symbol,
        exchange=SimpleNamespace(value=exchange),
        direction=SimpleNamespace(name=direction),
        offset=SimpleNamespace(name=offset),
        volume=1,
        price=70_000.0,
        datetime=datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("direction", "offset", "field"),
    [
        ("UNKNOWN", "OPEN", "direction"),
        ("LONG", "FORCECLOSE", "offset"),
    ],
)
def test_ctp_trade_handler_rejects_unknown_protocol_value_without_mutation(
    direction: str,
    offset: str,
    field: str,
) -> None:
    broker = CtpBroker(credentials())
    broker._positions = {
        "cu2609": ContractPosition(
            "cu2609",
            "SHFE",
            long_today=1,
            long_price=70_000,
        )
    }
    before = broker.get_positions()

    broker._on_trade(SimpleNamespace(data=raw_trade(direction=direction, offset=offset)))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert field in str(events[0].payload)
    assert broker.get_positions() == before


def test_ctp_order_handler_rejects_unknown_value_as_broker_error() -> None:
    broker, status = conversion_broker()
    raw = raw_order(status.NOTTRADED)
    raw.direction = SimpleNamespace(name="UNKNOWN")

    broker._on_order(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "direction" in str(events[0].payload)


def test_ctp_order_callback_surfaces_unexpected_conversion_failure() -> None:
    broker = CtpBroker(credentials())

    def fail_conversion(_raw: object) -> None:
        raise RuntimeError("unexpected adapter failure")

    broker._convert_order = fail_conversion

    broker._on_order(SimpleNamespace(data=object()))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "unexpected adapter failure" in str(events[0].payload)


def test_ctp_trade_callback_surfaces_unexpected_event_extraction_failure() -> None:
    broker = CtpBroker(credentials())

    class BrokenEvent:
        @property
        def data(self) -> object:
            raise RuntimeError("unexpected trade event failure")

    broker._on_trade(BrokenEvent())

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "unexpected trade event failure" in str(events[0].payload)


def test_tracked_gateway_blocks_ack_from_critical_ingress_until_handler_consumes(
    tmp_path,
    monkeypatch,
) -> None:
    import sys
    from types import ModuleType

    class OfficialCtpTdApi:
        def __init__(self, gateway):
            self.gateway = gateway
            self.reqid = 0
            self.order_ref = 0

        def _request(self):
            self.reqid += 1
            return self.reqid

        def authenticate(self):
            return self._request()

        def login(self):
            return self._request()

        def onRspUserLogin(self, *_args):
            return self._request()

        def onRspSettlementInfoConfirm(self, *_args):
            return self._request()

        def send_order(self, _request):
            self.order_ref += 1
            return self._request()

        def cancel_order(self, _request):
            return self._request()

        def query_account(self):
            return self._request()

        def query_position(self):
            return self._request()

    class OfficialCtpMdApi:
        def __init__(self, gateway):
            self.gateway = gateway

    class OfficialCtpGateway:
        def __init__(self, event_engine, gateway_name):
            self.event_engine = event_engine
            self.gateway_name = gateway_name
            self.td_api = OfficialCtpTdApi(self)
            self.md_api = OfficialCtpMdApi(self)

        def on_order(self, data):
            self.event_engine.append(("order", data))

        def on_trade(self, data):
            self.event_engine.append(("trade", data))

        def on_account(self, data):
            self.event_engine.append(("account", data))

        def on_tick(self, data):
            self.event_engine.append(("tick", data))

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

    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"
    broker.configure_order_submission_journal(
        tmp_path / "stress90_ctp_orders.json",
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    upstream: list[tuple[str, object]] = []
    gateway = broker._load_runtime()["CtpGateway"](upstream, "CTP")
    gateway._afuture_critical_ingress_callback = broker._mark_critical_upstream_pending

    td_api = gateway.td_api
    assert [
        td_api._afuture_allocate_request_id(),
        td_api.authenticate(),
        td_api.login(),
        td_api.onRspUserLogin({}, {}, 3, True),
        td_api.onRspSettlementInfoConfirm({}, {}, 4, True),
        td_api.send_order(object()),
        td_api.cancel_order(object()),
        td_api.query_account(),
        td_api.query_position(),
    ] == list(range(1, 10))
    assert td_api.reqid == 9
    assert td_api.order_ref == 1

    gateway.on_account(account_data(balance=500_000))
    assert upstream[-1][0] == "account"
    assert broker.acknowledge_critical_events() is False
    with pytest.raises(RuntimeError, match="acknowledged critical event boundary"):
        broker._require_acknowledged_critical_boundary()
    broker._on_account(SimpleNamespace(data=upstream.pop()[1]))
    assert [item.event_type for item in broker.poll_events()] == ["account"]
    assert broker.acknowledge_critical_events() is True

    gateway.on_order(object())
    assert broker.acknowledge_critical_events() is False
    broker._on_order(SimpleNamespace(data=upstream.pop()[1]))
    assert [item.event_type for item in broker.poll_events()] == ["broker_error"]
    assert broker.acknowledge_critical_events() is True

    gateway.on_trade(object())
    assert broker.acknowledge_critical_events() is False
    broker._on_trade(SimpleNamespace(data=upstream.pop()[1]))
    assert [item.event_type for item in broker.poll_events()] == ["broker_error"]
    assert broker.acknowledge_critical_events() is True

    gateway.on_tick(object())
    assert upstream[-1][0] == "tick"
    assert broker.acknowledge_critical_events() is True


def test_ctp_trade_handler_preserves_supported_trade_and_position_mirror() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"

    broker._on_trade(SimpleNamespace(data=raw_trade()))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["trade"]
    trade = events[0].payload
    assert trade.side is OrderSide.BUY
    assert trade.offset is Offset.OPEN
    assert broker.get_positions()[0].long_today == 1


def test_ctp_trade_callback_is_idempotent_within_session() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"
    event = SimpleNamespace(data=raw_trade())

    broker._on_trade(event)
    broker._on_trade(event)

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["trade"]
    assert broker.get_positions()[0].long_today == 1


def test_ctp_trade_idempotency_includes_exchange() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"

    broker._on_trade(
        SimpleNamespace(data=raw_trade(trade_id="SHARED-T1", symbol="same", exchange="DCE"))
    )
    broker._on_trade(
        SimpleNamespace(data=raw_trade(trade_id="SHARED-T1", symbol="same", exchange="SHFE"))
    )

    events = broker.poll_events()
    positions = sorted(broker.get_positions(), key=lambda position: position.exchange)
    assert [event.event_type for event in events] == ["trade", "trade"]
    assert [
        (position.symbol, position.exchange, position.long_today) for position in positions
    ] == [
        ("same", "DCE", 1),
        ("same", "SHFE", 1),
    ]


def test_ctp_position_snapshot_keeps_same_symbol_on_distinct_exchanges() -> None:
    broker = CtpBroker(credentials())
    raw_positions = [
        SimpleNamespace(
            symbol="same",
            exchange=SimpleNamespace(value=exchange),
            direction=SimpleNamespace(name="LONG"),
            volume=1,
            yd_volume=0,
            price=price,
        )
        for exchange, price in (("DCE", 100.0), ("SHFE", 200.0))
    ]

    broker._handle_position_snapshot(raw_positions)

    positions = sorted(broker.get_positions(), key=lambda position: position.exchange)
    assert [
        (position.symbol, position.exchange, position.long_price) for position in positions
    ] == [
        ("same", "DCE", 100.0),
        ("same", "SHFE", 200.0),
    ]


def test_ctp_serializes_snapshot_and_trade_position_truth_and_events() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"
    snapshot_conversion_started = Event()
    release_snapshot = Event()
    trade_prelock_reached = Event()
    trade_waiting_on_position = Event()
    trade_finished = Event()

    class ObservablePositionLock:
        """Expose real lock contention without adding a production test hook."""

        def __init__(self) -> None:
            self._lock = Lock()

        def __enter__(self) -> "ObservablePositionLock":
            if not self._lock.acquire(blocking=False):
                trade_waiting_on_position.set()
                self._lock.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self._lock.release()

    broker._position_lock = ObservablePositionLock()

    class BlockingDirection:
        @property
        def name(self) -> str:
            snapshot_conversion_started.set()
            release_snapshot.wait()
            return "LONG"

    class CheckpointTradeEvent:
        @property
        def data(self) -> SimpleNamespace:
            trade_prelock_reached.set()
            return raw_trade(
                trade_id="INTERLEAVED-T1",
                symbol="same",
                exchange="DCE",
            )

    snapshot = [
        SimpleNamespace(
            symbol="same",
            exchange=SimpleNamespace(value="DCE"),
            direction=BlockingDirection(),
            volume=1,
            yd_volume=0,
            price=100.0,
        )
    ]

    def process_trade() -> None:
        broker._on_trade(CheckpointTradeEvent())
        trade_finished.set()

    snapshot_thread = Thread(target=broker._handle_position_snapshot, args=(snapshot,))
    trade_thread = Thread(target=process_trade)
    snapshot_thread.start()
    try:
        assert snapshot_conversion_started.wait(1)
        trade_thread.start()
        assert trade_prelock_reached.wait(1)
        assert trade_waiting_on_position.wait(1)
        assert not trade_finished.is_set()
    finally:
        release_snapshot.set()
        snapshot_thread.join(2)
        if trade_thread.ident is not None:
            trade_thread.join(2)

    assert not snapshot_thread.is_alive()
    assert not trade_thread.is_alive()
    assert trade_finished.is_set()
    events = broker.poll_events()
    assert [event.event_type for event in events] == ["position_snapshot", "trade"]
    assert events[0].payload[0].long_today == 1
    position = broker.get_positions()[0]
    assert (position.long_today, position.long_price) == (2, 35_050.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("volume", 0),
        ("volume", -1),
        ("price", 0.0),
        ("price", -1.0),
        ("price", float("nan")),
        ("price", float("inf")),
    ],
)
def test_ctp_trade_handler_rejects_invalid_economic_value(
    field: str,
    value: object,
) -> None:
    broker = CtpBroker(credentials())
    raw = raw_trade()
    setattr(raw, field, value)

    broker._on_trade(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert field in str(events[0].payload)
    assert broker.get_positions() == []
