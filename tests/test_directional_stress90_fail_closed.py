from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from afuture.models import (
    ContractPosition,
    Offset,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Trade,
)

_CHINA = ZoneInfo("Asia/Shanghai")
_ACCOUNT_IDENTITY = "a" * 64
_ACCOUNT_EPOCH = "b" * 64


def _manager(tmp_path: Path, positions: list[ContractPosition]):
    from afuture.directional import DirectionalConfig
    from afuture.directional_activity import ContractActivity, DirectionalActivitySnapshot
    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        Stress90SeedStore,
        bind_stress90_account_identity,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260824",
        last_decision_digest="a" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "1" * 64},
        bootstrap_through_day="20260824",
        last_completed_input_day="20260823",
    )
    seed_path = tmp_path / "stress90_bootstrap_seed.json"
    state_path = tmp_path / "stress90_policy_state.json"
    Stress90SeedStore(seed_path).save_new(seed)
    Stress90PolicyStateStore(state_path).save(
        bind_stress90_account_identity(
            Stress90PolicyState.from_seed(seed),
            _ACCOUNT_IDENTITY,
            account_epoch=_ACCOUNT_EPOCH,
        )
    )

    class Broker:
        def __init__(self):
            self.orders = []

        def is_ready(self):
            return True

        def get_active_orders(self):
            return []

        def get_positions(self):
            return list(positions)

        def get_trading_day(self):
            return "20260825"

        def get_account_identity_digest(self):
            return _ACCOUNT_IDENTITY

        def send_order(self, request):
            self.orders.append(request)
            return f"order-{len(self.orders)}"

    broker = Broker()
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        broker,
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        historical_mode=True,
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=tmp_path / "missing_oi.json",
        activity_tracker=SimpleNamespace(
            completed_snapshot=DirectionalActivitySnapshot(
                "20260824",
                {
                    "A2612": ContractActivity(
                        "A2612",
                        "DCE",
                        "A",
                        "20260824",
                        10_000.0,
                        20_000.0,
                        datetime(2026, 8, 24, 14, 59, tzinfo=_CHINA),
                    )
                },
            )
        ),
    )
    manager._initialized = True
    return manager, broker, state_path


def test_missing_ohlc_or_oi_sends_zero_orders_and_is_risk_off_only_when_exposed(tmp_path: Path):
    flat, flat_broker, _ = _manager(tmp_path / "flat", [])
    flat_result = flat.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))
    assert flat_result.action == "reject"
    assert "OHLC" in flat_result.reason
    assert flat_broker.orders == []

    position = ContractPosition("A2612", "DCE", long_yesterday=2, long_price=100.0)
    exposed, exposed_broker, _ = _manager(tmp_path / "exposed", [position])
    exposed_result = exposed.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))
    assert exposed_result.action == "risk_off"
    assert "OHLC" in exposed_result.reason
    assert exposed_broker.orders == []


def test_invalid_policy_state_and_invalid_ctp_day_raise_for_engine_halt(tmp_path: Path):
    manager, _broker, state_path = _manager(tmp_path / "state", [])
    state_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(RuntimeError, match="state JSON"):
        manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))

    manager, broker, _ = _manager(tmp_path / "day", [])
    broker.get_trading_day = lambda: "2026-08-25"
    with pytest.raises(RuntimeError, match="current CTP trading day"):
        manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))


def test_active_orders_wait_before_any_candidate_or_broker_mutation(tmp_path: Path):
    manager, broker, _ = _manager(tmp_path, [])
    from afuture.directional_stress90_execution import prepare_stress90_execution_intent

    intent = prepare_stress90_execution_intent(
        manager.execution_intent_store,
        target_trading_day="20260825",
        daily_decision_digest="d" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        risk_overlay_digest=manager.risk_overlay_digest,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    request = OrderRequest(
        "A2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100.0,
        reference="directional:stress90:" + "d" * 12 + ":A",
    )
    active = Order("CTP.1_2_3", request, OrderStatus.NOT_TRADED)
    broker.get_active_orders = lambda: [active]
    broker.owns_order = lambda order_id: order_id == active.order_id
    broker.get_order_submission_identity = lambda order_id: SimpleNamespace(
        order_id=order_id,
        status="submitted",
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        products_manifest_digest=manager.runtime_products_manifest_digest,
        daily_decision_digest="d" * 64,
        execution_intent_digest=intent.source_digest,
        request=request,
        authorization_kind="candidate",
    )

    result = manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))

    assert result.action == "wait"
    assert broker.orders == []


@pytest.mark.parametrize("durable_status", ["terminal", "aborted_before_send"])
def test_active_order_cannot_regress_from_durable_final_status(
    tmp_path: Path,
    durable_status: str,
) -> None:
    manager, broker, _ = _manager(tmp_path, [])
    from afuture.directional_stress90_execution import prepare_stress90_execution_intent

    intent = prepare_stress90_execution_intent(
        manager.execution_intent_store,
        target_trading_day="20260825",
        daily_decision_digest="d" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        risk_overlay_digest=manager.risk_overlay_digest,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    request = OrderRequest(
        "A2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100.0,
        reference="directional:stress90:" + "d" * 12 + ":A",
    )
    active = Order("CTP.1_2_3", request, OrderStatus.NOT_TRADED)
    broker.get_active_orders = lambda: [active]
    broker.owns_order = lambda _order_id: True
    broker.get_order_submission_identity = lambda _order_id: SimpleNamespace(
        status=durable_status,
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        products_manifest_digest=manager.runtime_products_manifest_digest,
        daily_decision_digest="d" * 64,
        execution_intent_digest=intent.source_digest,
        request=request,
        authorization_kind="candidate",
    )

    with pytest.raises(RuntimeError, match="unknown active order"):
        manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))


def test_exact_active_identity_with_changed_request_fails_closed(tmp_path: Path):
    manager, broker, _ = _manager(tmp_path, [])
    from afuture.directional_stress90_execution import prepare_stress90_execution_intent

    intent = prepare_stress90_execution_intent(
        manager.execution_intent_store,
        target_trading_day="20260825",
        daily_decision_digest="d" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        risk_overlay_digest=manager.risk_overlay_digest,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    durable = OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 100.0)
    changed = replace(durable, volume=2)
    active = Order("CTP.1_2_3", changed, OrderStatus.NOT_TRADED)
    broker.get_active_orders = lambda: [active]
    broker.owns_order = lambda _order_id: True
    broker.get_order_submission_identity = lambda _order_id: SimpleNamespace(
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        daily_decision_digest="d" * 64,
        execution_intent_digest=intent.source_digest,
        request=durable,
    )

    with pytest.raises(RuntimeError, match="unknown active order"):
        manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))


def test_exact_owned_active_order_from_stale_decision_fails_closed(tmp_path: Path):
    manager, broker, _ = _manager(tmp_path, [])
    from afuture.directional_stress90_execution import prepare_stress90_execution_intent

    prepare_stress90_execution_intent(
        manager.execution_intent_store,
        target_trading_day="20260825",
        daily_decision_digest="d" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        risk_overlay_digest=manager.risk_overlay_digest,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    active = Order(
        "CTP.1_2_3",
        OrderRequest(
            "A2612",
            "DCE",
            OrderSide.BUY,
            Offset.OPEN,
            1,
            100.0,
            reference="directional:stress90:" + "e" * 12 + ":A",
        ),
        OrderStatus.NOT_TRADED,
    )
    broker.get_active_orders = lambda: [active]
    broker.owns_order = lambda _order_id: True
    broker.get_order_submission_identity = lambda order_id: SimpleNamespace(
        order_id=order_id,
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        daily_decision_digest="e" * 64,
        execution_intent_digest="f" * 64,
    )

    with pytest.raises(RuntimeError, match="unknown active order"):
        manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))


def test_unknown_active_order_fails_closed_instead_of_blanket_cancellation(tmp_path: Path):
    manager, broker, _ = _manager(tmp_path, [])
    request = OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 100.0)
    broker.get_active_orders = lambda: [Order("CTP.manual", request, OrderStatus.NOT_TRADED)]
    broker.owns_order = lambda _order_id: False

    with pytest.raises(RuntimeError, match="unknown active order"):
        manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))
    assert broker.orders == []


def _crash_fill_fixture(tmp_path: Path):
    from afuture.directional_stress90_execution import prepare_stress90_execution_intent

    manager, broker, _ = _manager(tmp_path, [])
    intent = prepare_stress90_execution_intent(
        manager.execution_intent_store,
        target_trading_day="20260825",
        daily_decision_digest="d" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        risk_overlay_digest=manager.risk_overlay_digest,
        current_lots={},
        margin_fitted_lots={"A2612": 1},
        symbol_products={"A2612": "A"},
    )
    manager.policy_state_store.load_required = lambda: SimpleNamespace(
        prepared_decision=SimpleNamespace(daily_decision_digest="d" * 64),
        live_account_identity_digest=_ACCOUNT_IDENTITY,
        live_account_epoch=_ACCOUNT_EPOCH,
    )
    request = OrderRequest(
        "A2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100.0,
        reference="directional:stress90:" + "d" * 12 + ":A",
    )
    order = Order("CTP.1_2_3", request, OrderStatus.FILLED, traded=1, average_price=100.0)
    broker.get_order = lambda _order_id: order
    broker.owns_order = lambda _order_id: True
    return manager, broker, intent, request


def test_crash_fill_exact_identity_must_bind_execution_intent_digest(tmp_path: Path):
    manager, broker, intent, request = _crash_fill_fixture(tmp_path)
    broker.get_order_submission_identity = lambda _order_id: SimpleNamespace(
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        products_manifest_digest=manager.runtime_products_manifest_digest,
        daily_decision_digest="d" * 64,
        execution_intent_digest="f" * 64,
        request=request,
        authorization_kind="candidate",
        filled_volume=0,
        fill_keys=(),
    )
    broker.get_session_trades = lambda: [
        Trade(
            "T1",
            "CTP.1_2_3",
            "A2612",
            "DCE",
            OrderSide.BUY,
            Offset.OPEN,
            1,
            100.0,
            datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
        )
    ]

    allowed, adopted, reason = manager.reconcile_authorized_crash_fills([], [], ())

    assert not allowed
    assert adopted == ()
    assert "unowned crash-window trade" in reason
    assert intent.source_digest != "f" * 64


def test_crash_reconcile_rejects_persisted_fill_key_with_changed_volume(
    tmp_path: Path,
) -> None:
    manager, broker, intent, request = _crash_fill_fixture(tmp_path)
    identity = "20260825:DCE:T1"
    broker.get_order_submission_identity = lambda _order_id: SimpleNamespace(
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        products_manifest_digest=manager.runtime_products_manifest_digest,
        daily_decision_digest="d" * 64,
        execution_intent_digest=intent.source_digest,
        request=request,
        authorization_kind="candidate",
        status="terminal",
        filled_volume=1,
        fill_keys=(identity,),
        fill_evidence=(
            {
                "key": identity,
                "order_id": "CTP.1_2_3",
                "symbol": "A2612",
                "exchange": "DCE",
                "side": "BUY",
                "offset": "OPEN",
                "volume": 1,
                "price": 100.0,
                "timestamp": "2026-08-25T09:00:00+08:00",
            },
        ),
    )
    broker.get_session_trades = lambda: [
        Trade(
            "T1",
            "CTP.1_2_3",
            "A2612",
            "DCE",
            OrderSide.BUY,
            Offset.OPEN,
            2,
            100.0,
            datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
        )
    ]
    remote = [ContractPosition("A2612", "DCE", long_today=2, long_price=100.0)]

    allowed, adopted, reason = manager.reconcile_authorized_crash_fills([], remote, ())

    assert allowed is False
    assert adopted == ()
    assert "fingerprint" in reason


def test_known_crash_fill_still_validates_persisted_fingerprint(
    tmp_path: Path,
) -> None:
    manager, broker, intent, request = _crash_fill_fixture(tmp_path)
    identity = "20260825:DCE:T1"
    broker.get_order_submission_identity = lambda _order_id: SimpleNamespace(
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        products_manifest_digest=manager.runtime_products_manifest_digest,
        daily_decision_digest="d" * 64,
        execution_intent_digest=intent.source_digest,
        request=request,
        authorization_kind="candidate",
        status="terminal",
        filled_volume=1,
        fill_keys=(identity,),
        fill_evidence=(
            {
                "key": identity,
                "order_id": "CTP.1_2_3",
                "symbol": "A2612",
                "exchange": "DCE",
                "side": "BUY",
                "offset": "OPEN",
                "volume": 1,
                "price": 100.0,
                "timestamp": "2026-08-25T09:00:00+08:00",
            },
        ),
    )
    broker.get_session_trades = lambda: [
        Trade(
            "T1",
            "CTP.1_2_3",
            "A2612",
            "DCE",
            OrderSide.BUY,
            Offset.OPEN,
            2,
            100.0,
            datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
        )
    ]
    positions = [ContractPosition("A2612", "DCE", long_today=1, long_price=100.0)]

    allowed, adopted, reason = manager.reconcile_authorized_crash_fills(
        positions,
        positions,
        (identity,),
    )

    assert allowed is False
    assert adopted == ()
    assert "fingerprint" in reason


def test_known_durable_fill_does_not_require_current_execution_intent(
    tmp_path: Path,
) -> None:
    manager, broker, intent, request = _crash_fill_fixture(tmp_path)
    identity = "20260825:DCE:T1"
    trade = Trade(
        "T1",
        "CTP.1_2_3",
        "A2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100.0,
        datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
    )
    broker.get_order_submission_identity = lambda _order_id: SimpleNamespace(
        account_identity_digest=_ACCOUNT_IDENTITY,
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        products_manifest_digest=manager.runtime_products_manifest_digest,
        daily_decision_digest="d" * 64,
        execution_intent_digest=intent.source_digest,
        request=request,
        authorization_kind="candidate",
        status="terminal",
        filled_volume=1,
        fill_keys=(identity,),
        fill_evidence=(
            {
                "key": identity,
                "order_id": "CTP.1_2_3",
                "symbol": "A2612",
                "exchange": "DCE",
                "side": "BUY",
                "offset": "OPEN",
                "volume": 1,
                "price": 100.0,
                "timestamp": "2026-08-25T09:00:00+08:00",
            },
        ),
    )
    broker.get_session_trades = lambda: [trade]
    manager.policy_state_store.load_required = lambda: SimpleNamespace(
        prepared_decision=SimpleNamespace(daily_decision_digest="e" * 64),
        live_account_identity_digest=_ACCOUNT_IDENTITY,
        live_account_epoch=_ACCOUNT_EPOCH,
    )
    positions = [ContractPosition("A2612", "DCE", long_today=1, long_price=100.0)]

    allowed, adopted, reason = manager.reconcile_authorized_crash_fills(
        positions,
        positions,
        (identity,),
    )

    assert allowed is True
    assert adopted == ()
    assert "reconcile" in reason


def test_fresh_recovered_fill_is_not_double_counted_against_durable_volume(
    tmp_path: Path,
) -> None:
    manager, broker, intent, request = _crash_fill_fixture(tmp_path)
    identity = "20260825:DCE:T1"
    trade = Trade(
        "T1",
        "CTP.1_2_3",
        "A2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100.0,
        datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
    )
    broker.get_order_submission_identity = lambda _order_id: SimpleNamespace(
        account_identity_digest=_ACCOUNT_IDENTITY,
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        products_manifest_digest=manager.runtime_products_manifest_digest,
        daily_decision_digest="d" * 64,
        execution_intent_digest=intent.source_digest,
        request=request,
        authorization_kind="candidate",
        status="terminal",
        filled_volume=1,
        fill_keys=(identity,),
        fill_evidence=(
            {
                "key": identity,
                "order_id": "CTP.1_2_3",
                "symbol": "A2612",
                "exchange": "DCE",
                "side": "BUY",
                "offset": "OPEN",
                "volume": 1,
                "price": 100.0,
                "timestamp": "2026-08-25T09:00:00+08:00",
            },
        ),
    )
    broker.get_session_trades = lambda: [trade]
    remote = [ContractPosition("A2612", "DCE", long_today=1, long_price=100.0)]

    allowed, adopted, reason = manager.reconcile_authorized_crash_fills([], remote, ())

    assert allowed is True
    assert adopted == (identity,)
    assert "reconcile" in reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "M2612"),
        ("exchange", "SHFE"),
        ("side", OrderSide.SELL),
        ("offset", Offset.CLOSE),
        ("volume", 2),
        ("price", 101.0),
    ],
)
def test_crash_fill_economics_must_match_persisted_exact_order(
    tmp_path: Path, field: str, value: object
):
    manager, broker, intent, request = _crash_fill_fixture(tmp_path)
    broker.get_order_submission_identity = lambda _order_id: SimpleNamespace(
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        products_manifest_digest=manager.runtime_products_manifest_digest,
        daily_decision_digest="d" * 64,
        execution_intent_digest=intent.source_digest,
        request=request,
        authorization_kind="candidate",
        filled_volume=0,
        fill_keys=(),
    )
    trade_fields = {
        "trade_id": "T1",
        "order_id": "CTP.1_2_3",
        "symbol": "A2612",
        "exchange": "DCE",
        "side": OrderSide.BUY,
        "offset": Offset.OPEN,
        "volume": 1,
        "price": 100.0,
        "timestamp": datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
    }
    trade_fields[field] = value
    broker.get_session_trades = lambda: [Trade(**trade_fields)]

    allowed, adopted, reason = manager.reconcile_authorized_crash_fills([], [], ())

    assert not allowed
    assert adopted == ()
    assert "durable" in reason


def test_crash_reconcile_accepts_exact_durable_risk_reduction_without_candidate_digest(
    tmp_path: Path,
) -> None:
    manager, broker, _intent, _opening_request = _crash_fill_fixture(tmp_path)
    request = OrderRequest(
        "A2612",
        "DCE",
        OrderSide.SELL,
        Offset.CLOSE,
        1,
        99.0,
        reference="directional:gross-guard",
    )
    order = Order("CTP.1_2_4", request, OrderStatus.FILLED, traded=1, average_price=99.0)
    broker.get_order = lambda _order_id: order
    broker.get_order_submission_identity = lambda _order_id: SimpleNamespace(
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        products_manifest_digest=manager.runtime_products_manifest_digest,
        daily_decision_digest="a" * 64,
        execution_intent_digest="b" * 64,
        request=request,
        authorization_kind="risk_reduction",
        filled_volume=0,
        fill_keys=(),
    )
    broker.get_session_trades = lambda: [
        Trade(
            "RISK-T1",
            "CTP.1_2_4",
            "A2612",
            "DCE",
            OrderSide.SELL,
            Offset.CLOSE,
            1,
            99.0,
            datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
        )
    ]
    local = [ContractPosition("A2612", "DCE", long_today=1, long_price=100.0)]

    allowed, adopted, reason = manager.reconcile_authorized_crash_fills(local, [], ())

    assert allowed is True
    assert adopted == ("20260825:DCE:RISK-T1",)
    assert "reconcile" in reason


def test_risk_reduction_crash_reconcile_does_not_require_candidate_execution_intent(
    tmp_path: Path,
) -> None:
    manager, broker, _ = _manager(tmp_path, [])
    assert manager.execution_intent_store.load_record() is None
    request = OrderRequest(
        "A2612",
        "DCE",
        OrderSide.SELL,
        Offset.CLOSE,
        1,
        99.0,
        reference="directional:gross-guard",
    )
    order = Order("CTP.1_2_4", request, OrderStatus.FILLED, traded=1, average_price=99.0)
    broker.get_order = lambda _order_id: order
    broker.owns_order = lambda _order_id: True
    broker.get_order_submission_identity = lambda _order_id: SimpleNamespace(
        target_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest=manager.runtime_policy_definition_digest,
        products_manifest_digest=manager.runtime_products_manifest_digest,
        daily_decision_digest="a" * 64,
        execution_intent_digest="b" * 64,
        request=request,
        authorization_kind="risk_reduction",
        filled_volume=0,
        fill_keys=(),
    )
    broker.get_session_trades = lambda: [
        Trade(
            "RISK-T1",
            "CTP.1_2_4",
            "A2612",
            "DCE",
            OrderSide.SELL,
            Offset.CLOSE,
            1,
            99.0,
            datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
        )
    ]
    local = [ContractPosition("A2612", "DCE", long_today=1, long_price=100.0)]

    allowed, adopted, reason = manager.reconcile_authorized_crash_fills(local, [], ())

    assert allowed is True
    assert adopted == ("20260825:DCE:RISK-T1",)
    assert "reconcile" in reason
