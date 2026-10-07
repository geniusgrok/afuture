"""Offline rate-query boundary tests; no gateway start, login or order calls."""

from time import monotonic
from types import SimpleNamespace

import pytest

from afuture.broker.ctp import CtpBroker, CtpCredentials
from afuture.broker.sim import SimBroker
from afuture.engine import TradingEngine
from afuture.fees import calculate_commission
from afuture.metadata import validate_contract_metadata
from afuture.models import Offset, OrderRequest, OrderSide, OrderType, RuntimeMode
from afuture.risk import RiskConfig, RiskManager
from afuture.state import RuntimeState, StateStore


@pytest.fixture
def rate_boundary():
    broker = CtpBroker(CtpCredentials(*["synthetic"] * 8))
    margin = {
        "InstrumentID": "al2611",
        "IsRelative": 0,
        "LongMarginRatioByMoney": 0.12,
        "ShortMarginRatioByMoney": 0.13,
        "LongMarginRatioByVolume": 0.0,
        "ShortMarginRatioByVolume": 0.0,
    }
    commission = {
        "InstrumentID": "al2611",
        "OpenRatioByVolume": 2.0,
        "OpenRatioByMoney": 0.0001,
        "CloseRatioByVolume": 3.0,
        "CloseRatioByMoney": 0.0002,
        "CloseTodayRatioByVolume": 0.0,
        "CloseTodayRatioByMoney": 0.0,
    }

    class Td:
        def __init__(self):
            self.reqid = 0
            self._afuture_rate_waiters = {}

        def deliver(self, request, reqid, row):
            assert request["InstrumentID"] == "al2611"
            waiter = self._afuture_rate_waiters[reqid]
            waiter["rows"].append(dict(row))
            waiter["event"].set()
            return 0

        def reqQryInstrumentMarginRate(self, request, reqid):
            return self.deliver(request, reqid, margin)

        def reqQryInstrumentCommissionRate(self, request, reqid):
            return self.deliver(request, reqid, commission)

    td = Td()
    broker.is_ready = lambda: True
    broker._main_engine = SimpleNamespace(
        get_gateway=lambda _: SimpleNamespace(td_api=td),
        get_all_contracts=lambda: [
            SimpleNamespace(
                symbol="al2611", exchange=SimpleNamespace(value="SHFE"), size=5, pricetick=5
            )
        ],
    )
    return broker, margin, commission, td


@pytest.mark.parametrize(
    "field",
    [
        "OpenRatioByVolume",
        "OpenRatioByMoney",
        "CloseRatioByVolume",
        "CloseRatioByMoney",
        "CloseTodayRatioByVolume",
        "CloseTodayRatioByMoney",
    ],
)
def test_incomplete_commission_is_rejected_instead_of_zero_default(rate_boundary, field):
    broker, _, commission, td = rate_boundary
    del commission[field]
    with pytest.raises(RuntimeError, match=field):
        broker.get_live_contract_specs(["al2611"])
    assert td._afuture_rate_waiters == {}


@pytest.mark.parametrize(
    "field",
    [
        "LongMarginRatioByMoney",
        "ShortMarginRatioByMoney",
        "LongMarginRatioByVolume",
        "ShortMarginRatioByVolume",
    ],
)
def test_incomplete_margin_does_not_claim_absent_fixed_margin(rate_boundary, field):
    broker, margin, _, _ = rate_boundary
    del margin[field]
    with pytest.raises(RuntimeError, match=field):
        broker.get_live_contract_specs(["al2611"])


@pytest.mark.parametrize("value", [None, True, "", float("nan"), float("inf"), -1])
def test_invalid_raw_rate_is_rejected(rate_boundary, value):
    broker, _, commission, _ = rate_boundary
    commission["OpenRatioByMoney"] = value
    with pytest.raises(RuntimeError, match="OpenRatioByMoney"):
        broker.get_live_contract_specs(["al2611"])


def test_complete_rates_preserve_units_and_explicit_zero_close_today(rate_boundary):
    broker, _, _, _ = rate_boundary
    spec = broker.get_live_contract_specs(["al2611"])["al2611"]
    assert (spec.margin_rate_long, spec.margin_rate_short) == (0.12, 0.13)
    assert calculate_commission(spec, Offset.OPEN, 20_000, 2) == pytest.approx(24)
    assert calculate_commission(spec, Offset.CLOSE, 20_000, 2) == pytest.approx(46)
    assert calculate_commission(spec, Offset.CLOSE_TODAY, 20_000, 2) == 0
    assert validate_contract_metadata({spec.symbol: spec}, {spec.symbol: spec}).allowed


def test_fixed_per_lot_margin_remains_unsupported(rate_boundary):
    broker, margin, _, _ = rate_boundary
    margin["LongMarginRatioByVolume"] = 100
    with pytest.raises(RuntimeError, match="fixed-per-lot margin is unsupported"):
        broker.get_live_contract_specs(["al2611"])


@pytest.mark.parametrize("value", [1, True, None, "0", 2])
def test_unresolved_relative_margin_is_not_certified_as_absolute(rate_boundary, value):
    broker, margin, _, _ = rate_boundary
    margin["IsRelative"] = value
    with pytest.raises(RuntimeError, match="IsRelative"):
        broker.get_live_contract_specs(["al2611"])


def test_absent_relative_margin_flag_does_not_imply_absolute(rate_boundary):
    broker, margin, _, _ = rate_boundary
    del margin["IsRelative"]
    with pytest.raises(RuntimeError, match="IsRelative"):
        broker.get_live_contract_specs(["al2611"])


def test_query_cleanup_is_preserved_on_upstream_error(rate_boundary):
    broker, _, _, td = rate_boundary

    def fail_query(_request, reqid):
        waiter = td._afuture_rate_waiters[reqid]
        waiter["error"] = "synthetic failure"
        waiter["event"].set()
        return 0

    td.reqQryInstrumentCommissionRate = fail_query
    with pytest.raises(RuntimeError, match="synthetic failure"):
        broker._query_rate(td, "commission", "al2611", monotonic() + 1)
    assert td._afuture_rate_waiters == {}


def test_partial_rates_cannot_clear_restored_kill_switch(rate_boundary, tmp_path):
    raw_broker, _, commission, _ = rate_boundary
    specs = raw_broker.get_live_contract_specs(["al2611"])
    del commission["OpenRatioByMoney"]
    broker = SimBroker(500_000, specs)
    broker.get_live_contract_specs = raw_broker.get_live_contract_specs
    store = StateStore(tmp_path / "synthetic-state.json")
    store.save(RuntimeState(kill_switch=True, positions=[]))
    engine = TradingEngine(
        broker, [], specs, RiskManager(RiskConfig()), store, require_live_metadata=True
    )
    engine.start()
    assert engine.halted and not engine.state.metadata_verified
    assert not engine.clear_kill_switch_after_reconcile()


@pytest.mark.parametrize("invalid", [False, True])
def test_new_day_revalidates_rates_without_clearing_recovery_gate(rate_boundary, tmp_path, invalid):
    raw_broker, _, commission, _ = rate_boundary
    specs = raw_broker.get_live_contract_specs(["al2611"])
    broker = SimBroker(500_000, specs)
    broker._trading_day = "20260821"
    broker.get_live_contract_specs = raw_broker.get_live_contract_specs
    store = StateStore(tmp_path / "synthetic-state.json")
    engine = TradingEngine(
        broker, [], specs, RiskManager(RiskConfig()), store, require_live_metadata=True
    )
    engine.start()
    assert not engine.halted and engine.state.metadata_verified
    if invalid:
        del commission["OpenRatioByMoney"]
    broker._trading_day = "20260822"
    engine._handle_account_event(broker.get_account())
    restored = store.load()
    assert restored.trading_day == "20260822"
    assert engine.state.metadata_verified is not invalid
    assert engine.halted is invalid
    if invalid:
        assert restored.kill_switch and restored.runtime_mode == RuntimeMode.HALTED.value
        assert not engine.clear_kill_switch_after_reconcile()
    else:
        assert not restored.kill_switch and restored.runtime_mode == RuntimeMode.RUNNING.value


def test_rate_rejection_preserves_independent_reduction_submission(rate_boundary, tmp_path):
    rate_broker, _, commission, td = rate_boundary
    broker = CtpBroker(CtpCredentials(*["synthetic"] * 8))
    broker.configure_order_submission_journal(
        tmp_path / "synthetic-orders.json",
        policy_id="directional.stress90",
        policy_definition_digest="a" * 64,
        products_manifest_digest="b" * 64,
    )
    broker.is_ready = rate_broker.is_ready
    broker._main_engine = rate_broker._main_engine
    del commission["OpenRatioByMoney"]
    with pytest.raises(RuntimeError, match="OpenRatioByMoney"):
        broker.get_live_contract_specs(["al2611"])
    td.frontid, td.sessionid, td.order_ref = 7, 11, 40
    td.getTradingDay = lambda: "20260821"
    broker._to_vnpy_order = lambda request: request
    calls = []

    def submit(request, _gateway):
        td.order_ref += 1
        calls.append(request)
        return f"CTP.7_11_{td.order_ref}"

    broker._main_engine.send_order = submit
    opening = OrderRequest(
        "al2611", "SHFE", OrderSide.BUY, Offset.OPEN, 1, 20_000, reference="directional:synthetic"
    )
    with pytest.raises(RuntimeError, match="candidate context is missing"):
        broker.send_order(opening)
    reduction = OrderRequest(
        "al2611",
        "SHFE",
        OrderSide.SELL,
        Offset.CLOSE_TODAY,
        1,
        20_000,
        OrderType.FAK,
        "directional:synthetic-protection",
    )
    assert broker.send_order(reduction) == "CTP.7_11_41"
    assert calls == [reduction]
