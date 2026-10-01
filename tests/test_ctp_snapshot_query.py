"""Production tracked SDK callback tests; no field/real-counter claims."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from afuture.broker.ctp import CtpBroker, CtpCredentials
from afuture.broker.ctp_snapshot_query import CtpSnapshotQuery


@pytest.fixture
def native_boundary(monkeypatch):
    broker = CtpBroker(
        CtpCredentials(
            "user",
            "secret",
            "9999",
            "tcp://test-td",
            "tcp://test-md",
            "app",
            "auth",
            "test",
            account_id="acct",
            currency_id="CNY",
            investor_id="",
        )
    )
    broker._trading_day = "20260922"

    class Td:
        def __init__(self, gateway):
            self.gateway = gateway
            self.positions = {}
            self.reqid = 0
            self.day = "20260922"
            self.status = 0
            self.missing_contract = False

        def getTradingDay(self):
            return self.day

        def reqQryTradingAccount(self, _request, _request_id):
            return self.status

        def reqQryInvestorPosition(self, _request, _request_id):
            return self.status

        def query_account(self):
            self.reqid += 1
            return self.reqQryTradingAccount({}, self.reqid)

        def query_position(self):
            self.reqid += 1
            return self.reqQryInvestorPosition({}, self.reqid)

        def onRspQryTradingAccount(self, data, _error, _reqid, _last):
            self.gateway.on_account(
                SimpleNamespace(
                    accountid=data["AccountID"],
                    balance=data["Balance"],
                    available=data["Available"],
                )
            )

        def onRspQryInvestorPosition(self, data, _error, _reqid, last):
            assert not last
            if self.missing_contract:
                return
            key = data["InstrumentID"], data["PosiDirection"]
            p = self.positions.setdefault(
                key,
                SimpleNamespace(
                    symbol=data["InstrumentID"],
                    exchange=SimpleNamespace(value=data["ExchangeID"]),
                    direction=SimpleNamespace(
                        name="LONG" if data["PosiDirection"] == "2" else "SHORT"
                    ),
                    volume=0,
                    yd_volume=0,
                    price=100.0,
                ),
            )
            p.volume += data["Position"]
            if data["PositionDate"] == "2":
                p.yd_volume += data["Position"]

    class Md:
        def __init__(self, gateway):
            self.gateway = gateway

    class Gateway:
        def __init__(self, event_engine, gateway_name):
            self.event_engine = event_engine
            self.gateway_name = gateway_name
            self.td_api = Td(self)
            self.md_api = Md(self)

        def on_position(self, _data):
            pass

        def on_account(self, data):
            broker._on_account(SimpleNamespace(data=data))

    names = [
        "vnpy",
        "vnpy.event",
        "vnpy.trader",
        "vnpy.trader.constant",
        "vnpy.trader.engine",
        "vnpy.trader.event",
        "vnpy.trader.object",
        "vnpy_ctp",
        "vnpy_ctp.gateway",
        "vnpy_ctp.gateway.ctp_gateway",
    ]
    modules = {name: ModuleType(name) for name in names}
    modules["vnpy.event"].EventEngine = object
    for name in ("Direction", "Exchange", "Status", "Offset", "OrderType"):
        setattr(modules["vnpy.trader.constant"], name, type(name, (), {}))
    modules["vnpy.trader.engine"].MainEngine = object
    for name in ("ACCOUNT", "ORDER", "TICK", "TRADE"):
        setattr(modules["vnpy.trader.event"], f"EVENT_{name}", name.lower())
    modules["vnpy.trader.object"].OrderRequest = object
    modules["vnpy.trader.object"].SubscribeRequest = object
    modules["vnpy_ctp.gateway.ctp_gateway"].CtpGateway = Gateway
    modules["vnpy_ctp.gateway.ctp_gateway"].CtpTdApi = Td
    modules["vnpy_ctp.gateway.ctp_gateway"].CtpMdApi = Md
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    gateway = broker._load_runtime()["CtpGateway"]([], "CTP")
    gateway._afuture_position_snapshot_callback = broker._handle_position_snapshot
    gateway._afuture_account_cash_flow_callback = broker._handle_account_cash_flow
    gateway._afuture_critical_ingress_callback = broker._mark_critical_upstream_pending
    return broker, gateway.td_api


def position(**changes):
    row = dict(
        BrokerID="9999",
        InvestorID="user",
        TradingDay="20260922",
        InstrumentID="rb2610",
        ExchangeID="SHFE",
        PosiDirection="2",
        HedgeFlag="1",
        PositionDate="1",
        Position=2,
    )
    return {**row, **changes}


def account(**changes):
    row = dict(
        BrokerID="9999",
        AccountID="acct",
        CurrencyID="CNY",
        TradingDay="20260922",
        SettlementID=1,
        PreBalance=100000.0,
        Deposit=0.0,
        Withdraw=0.0,
        Balance=100000.0,
        Available=100000.0,
    )
    return {**row, **changes}


def test_native_position_query_publishes_only_complete_deduplicated_generation(native_boundary):
    broker, td = native_boundary
    assert td.query_position() == 0
    td.onRspQryInvestorPosition(position(), {}, 1, False)
    td.onRspQryInvestorPosition(position(), {}, 1, False)
    assert broker.snapshot_marker() == (0, 0)
    assert broker.get_positions() == []
    td.onRspQryInvestorPosition(position(PositionDate="2", Position=3), {}, 1, True)
    assert broker.snapshot_marker() == (0, 1)
    p = broker.get_positions()[0]
    assert (p.long_today, p.long_yesterday) == (2, 3)
    assert not broker._snapshot_query_error


def test_complete_empty_position_query_can_clear_previous_snapshot(native_boundary):
    broker, td = native_boundary
    td.query_position()
    td.onRspQryInvestorPosition(position(), {}, 1, True)
    td.query_position()
    td.onRspQryInvestorPosition({}, {}, 2, True)
    assert broker.snapshot_marker() == (0, 2)
    assert not broker.get_positions()


@pytest.mark.parametrize(
    "changes",
    [
        {"BrokerID": "foreign"},
        {"InvestorID": "foreign"},
        {"TradingDay": "20260923"},
        {"Position": True},
        {"Position": -1},
        {"Position": 1.5},
        {"HedgeFlag": "2"},
        {"PosiDirection": "1"},
        {"PositionDate": "3"},
        {"ExchangeID": ""},
    ],
)
def test_bad_native_position_identity_never_publishes_partial_snapshot(native_boundary, changes):
    broker, td = native_boundary
    td.query_position()
    td.onRspQryInvestorPosition(position(), {}, 1, False)
    td.onRspQryInvestorPosition(position(**changes), {}, 1, True)
    assert broker.snapshot_marker() == (0, 0)
    assert broker._snapshot_query_error
    assert any(e.event_type == "broker_error" for e in broker.poll_events())


@pytest.mark.parametrize(
    "failure", ["wrong_request", "double_last", "conflict", "missing_contract", "day_changed"]
)
def test_native_generation_and_sdk_integrity_failures_are_sticky(native_boundary, failure):
    broker, td = native_boundary
    td.query_position()
    if failure == "double_last":
        td.onRspQryInvestorPosition(position(), {}, 1, True)
        before = broker.snapshot_marker()
        td.onRspQryInvestorPosition({}, {}, 1, True)
    else:
        before = broker.snapshot_marker()
        td.onRspQryInvestorPosition(position(), {}, 1, False)
        td.missing_contract = failure == "missing_contract"
        if failure == "day_changed":
            td.day = "20260923"
        td.onRspQryInvestorPosition(
            position(Position=3 if failure == "conflict" else 2),
            {},
            2 if failure == "wrong_request" else 1,
            True,
        )
    assert broker.snapshot_marker() == before
    assert broker._snapshot_query_error


def test_account_event_waits_for_same_request_end(native_boundary):
    broker, td = native_boundary
    td.query_account()
    td.onRspQryTradingAccount(account(), {}, 1, False)
    assert broker.snapshot_marker() == (0, 0)
    assert not broker.account_identity_verified
    td.onRspQryTradingAccount({}, {}, 1, True)
    assert broker.snapshot_marker() == (1, 0)
    assert broker.account_identity_verified


@pytest.mark.parametrize(
    "changes",
    [
        {"AccountID": "foreign"},
        {"CurrencyID": "USD"},
        {"TradingDay": "20260923"},
        {"PreBalance": float("nan")},
    ],
)
def test_account_row_cannot_be_promoted_before_complete_identity_validation(
    native_boundary, changes
):
    broker, td = native_boundary
    td.query_account()
    td.onRspQryTradingAccount(account(**changes), {}, 1, True)
    assert broker.snapshot_marker() == (0, 0)
    assert not broker.account_identity_verified
    assert broker._snapshot_query_error


def test_timed_out_partial_generation_cannot_leak_into_next_query():
    clock = [0.0]
    guard = CtpSnapshotQuery(
        "position", broker_id="9999", investor_id="user", clock=lambda: clock[0]
    )
    assert guard.begin(1, "20260922")
    assert guard.observe(position(), {}, 1, False, trading_day="20260922") is None
    assert not guard.begin(2, "20260922")
    clock[0] = 31.0
    assert guard.begin(3, "20260922")
    assert guard.observe(position(), {}, 1, True, trading_day="20260922") is None
    assert guard.observe({}, {}, 3, True, trading_day="20260922") == ()
