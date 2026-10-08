"""Authenticated replacement rolls use later sessions without creating new risk."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_directional_stress90_runtime import _CHINA, _mechanical_manager

from afuture.broker.ctp_order_journal import (
    CtpOrderSubmissionEntry,
    CtpOrderSubmissionJournal,
    ctp_order_fill_evidence,
)
from afuture.directional_activity import ContractActivity, DirectionalActivitySnapshot
from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST
from afuture.directional_stress90_execution import Stress90ExecutionIntentStore
from afuture.directional_stress90_state import Stress90DecisionInputs, prepare_stress90_decision
from afuture.models import (
    ContractInfo,
    ContractPosition,
    ContractSpec,
    Offset,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Trade,
)
from afuture.position import PositionBook
from afuture.runtime_calendar import RuntimeTradingCalendar


def _roll_manager(tmp_path: Path, *, target_weight=0.4, short=False):
    old = ContractPosition("A2612", "DCE", long_yesterday=5, long_price=1000.0)
    if short:
        old = ContractPosition("A2612", "DCE", short_yesterday=5, short_price=1000.0)
    manager, broker, intent_path, now = _mechanical_manager(
        tmp_path, target_weight=target_weight, positions=[old]
    )
    new = ContractInfo("A2701", "DCE", "A", "2027-01-15")
    manager._catalog.append(new)
    manager._catalog_by_symbol[new.symbol] = new
    manager._ticks[new.symbol] = replace(
        manager._ticks[old.symbol],
        symbol=new.symbol,
        bid_price=1999.0,
        ask_price=2001.0,
        last_price=2000.0,
        limit_up=2200.0,
        limit_down=1800.0,
    )
    manager._specs[new.symbol] = ContractSpec(new.symbol, "DCE", 10.0, 1.0, 0.10, 0.10)
    rows = dict(manager.activity_tracker.completed_snapshot.contracts)
    rows[new.symbol] = ContractActivity(new.symbol, "DCE", "A", "20260824", 40_000.0, 60_000.0, now)
    manager.activity_tracker.completed_snapshot = DirectionalActivitySnapshot("20260824", rows)
    weights = dict(manager._prepare_decision_for_current_day("20260825").base_weights)
    weights["A"] = -abs(target_weight) if short else abs(target_weight)
    flow = -1 if short else 1
    history = [100.0 * (1.002 if not short else 0.998) ** i for i in range(21)]
    prepared = prepare_stress90_decision(
        manager.policy_state_store,
        Stress90DecisionInputs(
            previous_target_trading_day="20260824",
            target_trading_day="20260825",
            base_weights=weights,
            completed_close_history={p: history for p in weights},
            completed_oi_flow={p: flow for p in manager.policy_state_store.definition.oi_products},
            completed_close_day="20260824",
            completed_oi_day="20260824",
        ),
    )
    manager._prepare_decision_for_current_day = lambda current, **kwargs: prepared
    manager.risk_manager.runtime_calendar = RuntimeTradingCalendar.load()
    broker.get_unresolved_order_submission_identities = lambda: ()
    broker.get_order_submission_identities = lambda: ()
    broker.get_order = lambda _order_id: None
    broker.has_pending_critical_events = lambda: False
    return manager, broker, intent_path, now


def _at(manager, *, price=2000.0):
    now = datetime(2026, 8, 25, 14, 0, tzinfo=_CHINA)
    manager._ticks = {
        symbol: replace(tick, timestamp=now) for symbol, tick in manager._ticks.items()
    }
    manager._ticks["A2701"] = replace(
        manager._ticks["A2701"], bid_price=price - 1, ask_price=price + 1, last_price=price
    )
    return now


def _request(manager, *, volume=1, price=2001.0, short=False):
    intent = manager.execution_intent_store.load_required_record().intent
    return OrderRequest(
        "A2701",
        "DCE",
        OrderSide.SELL if short else OrderSide.BUY,
        Offset.OPEN,
        volume,
        price,
        OrderType.FAK,
        f"directional:stress90:{intent.daily_decision_digest[:12]}:A",
    )


def _fill(manager, broker, tmp_path, request, *, volume, price, offset=Offset.OPEN):
    """Use the production durable journal and fill book; all Broker I/O is synthetic."""
    intent = manager.execution_intent_store.load_required_record().intent
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    record = journal.load_runtime()
    sequence = 1 if record is None else len(record.all_entries) + 1
    order_id = f"CTP.7_11_{sequence}"
    if offset is not Offset.OPEN:
        request = replace(request, offset=offset, side=OrderSide.SELL)
    entry = CtpOrderSubmissionEntry(
        sequence=sequence,
        account_identity_digest=intent.account_identity_digest,
        policy_id=manager.runtime_policy_id,
        policy_definition_digest=intent.policy_definition_digest,
        products_manifest_digest=intent.products_manifest_digest,
        target_trading_day=intent.target_trading_day,
        daily_decision_digest=intent.daily_decision_digest,
        execution_intent_digest=intent.source_digest,
        transition={
            "freeze_authorized_lots": dict(intent.freeze_authorized_lots),
            "transitions": [],
        },
        front_id=7,
        session_id=11,
        order_ref=sequence,
        order_id=order_id,
        request=request,
        status="prepared",
        authorization_kind="risk_reduction" if offset is not Offset.OPEN else "candidate",
    )
    journal.prepare(entry)
    trade = Trade(
        f"T{sequence}",
        order_id,
        request.symbol,
        request.exchange,
        request.side,
        request.offset,
        volume,
        price,
        _at(manager),
    )
    journal.apply_updates(
        statuses={order_id: "terminal"},
        fills={order_id: (ctp_order_fill_evidence(intent.target_trading_day, trade),)},
    )
    book = PositionBook(broker.positions)
    book.apply_trade(trade)
    broker.positions = book.all()
    broker.get_order_submission_identities = lambda: journal.load_runtime().all_entries
    broker.get_order = lambda order_id: Order(
        order_id,
        journal.get_entry(order_id).request,
        OrderStatus.CANCELLED,
        traded=journal.get_entry(order_id).filled_volume,
    )
    return journal


def _windows(manager, request, now):
    return manager._opening_session_windows(
        "A", request, manager._ticks["A2701"], manager._specs["A2701"], now
    )


def _ctp_broker(manager, fake, journal_path, *, order_ref=40, before_official_send=None):
    from test_ctp_order_submission_boundary import _attach_gateway, _credentials

    from afuture.broker.ctp import CtpBroker

    intent = manager.execution_intent_store.load_required_record().intent
    account = fake.get_account()
    broker = CtpBroker(_credentials())
    broker.get_account_identity_digest = lambda: intent.account_identity_digest
    broker.configure_order_submission_journal(
        journal_path,
        policy_id=manager.runtime_policy_id,
        policy_definition_digest=intent.policy_definition_digest,
        products_manifest_digest=intent.products_manifest_digest,
    )
    td_api, calls = _attach_gateway(broker, before_official_send=before_official_send)
    td_api.order_ref = order_ref
    broker._main_engine.get_all_active_orders = lambda: []
    broker.get_account = lambda: account
    broker.health_error = lambda: None  # synthetic fresh/complete broker health evidence
    broker.metadata_query_blocks = False  # use the fixture's exact static metadata
    broker.get_order = lambda order_id: Order(
        order_id,
        broker.get_order_submission_identity(order_id).request,
        OrderStatus.CANCELLED,
        traded=broker.get_order_submission_identity(order_id).filled_volume,
    )
    return broker, calls


def test_authenticated_roll_after_reduction_opens_in_later_session(tmp_path: Path):
    manager, broker, path, now = _roll_manager(tmp_path)
    first = manager.maybe_rebalance(now)
    assert first.action == "reduce"
    original = Stress90ExecutionIntentStore(path).load_required_record()
    assert original.intent.transitions[0].max_replacement_notional == 50_000.0
    assert original.intent.transitions[0].kind == "same_product_roll"
    assert manager.policy_state_store.load_required().prepared_decision is not None
    broker.positions = []  # authoritative fill-owned position snapshot after old-leg fills
    result = manager.maybe_rebalance(_at(manager))
    assert result.action == "open", result.reason
    assert broker.orders[-1].symbol == "A2701"
    assert broker.orders[-1].volume == 2
    assert broker.orders[-1].offset is Offset.OPEN
    assert Stress90ExecutionIntentStore(path).load_required_record() == original


def test_entire_roll_request_above_original_budget_is_rejected_without_splitting(tmp_path):
    manager, broker, path, now = _roll_manager(tmp_path, target_weight=2.0)
    assert manager.maybe_rebalance(now).action == "reduce"
    original = Stress90ExecutionIntentStore(path).load_required_record()
    assert original.intent.freeze_authorized_lots == {"A2701": 10}
    broker.positions = []
    result = manager.maybe_rebalance(_at(manager))
    assert result.action == "reject"
    assert "session" in result.reason
    assert len(broker.orders) == 1
    assert Stress90ExecutionIntentStore(path).load_required_record() == original


def test_original_first_window_keeps_existing_roll_sizing(tmp_path):
    manager, broker, _, now = _roll_manager(tmp_path, target_weight=2.0)
    assert manager.maybe_rebalance(now).action == "reduce"
    broker.positions = []
    assert manager.maybe_rebalance(now).action == "open"
    assert broker.orders[-1].volume == 10


def test_partial_old_close_must_finish_before_later_session_open(tmp_path):
    manager, broker, _, now = _roll_manager(tmp_path)
    assert manager.maybe_rebalance(now).action == "reduce"
    broker.positions = [ContractPosition("A2612", "DCE", long_yesterday=2, long_price=1000)]
    result = manager.maybe_rebalance(_at(manager))
    assert result.action == "reduce"
    assert broker.orders[-1].symbol == "A2612"
    assert all(order.offset is not Offset.OPEN for order in broker.orders)


def test_cumulative_expensive_fills_cannot_release_budget_when_quote_falls(tmp_path):
    manager, broker, path, now = _roll_manager(tmp_path, target_weight=0.6)
    assert manager.maybe_rebalance(now).action == "reduce"
    original = Stress90ExecutionIntentStore(path).load_required_record()
    broker.positions = []
    for _ in range(2):
        _fill(manager, broker, tmp_path, _request(manager, price=2401), volume=1, price=2400)
    # Incorrect/settlement snapshot basis must not override exact durable fill prices.
    broker.positions = [replace(broker.positions[0], long_price=1000)]
    result = manager.maybe_rebalance(_at(manager, price=1000))
    assert result.action == "reject"
    assert len(broker.orders) == 1
    assert Stress90ExecutionIntentStore(path).load_required_record() == original


def test_new_leg_risk_reduction_never_recycles_roll_budget(tmp_path):
    manager, broker, _, now = _roll_manager(tmp_path)
    assert manager.maybe_rebalance(now).action == "reduce"
    broker.positions = []
    _fill(manager, broker, tmp_path, _request(manager, volume=2), volume=2, price=2000)
    _fill(
        manager,
        broker,
        tmp_path,
        _request(manager, price=1999),
        volume=1,
        price=1999,
        offset=Offset.CLOSE,
    )
    assert broker.positions[0].long_total == 1
    assert manager.maybe_rebalance(_at(manager)).action == "reject"
    assert len(broker.orders) == 1


@pytest.mark.parametrize("fault", ["unknown", "active", "pending", "late_fill"])
def test_child_rechecks_new_broker_facts_after_authorization(tmp_path, fault):
    manager, broker, _, now = _roll_manager(tmp_path)
    assert manager.maybe_rebalance(now).action == "reduce"
    broker.positions = []

    def authorize(_requests):
        if fault == "unknown":
            broker.get_unresolved_order_submission_identities = lambda: (object(),)
        elif fault == "active":
            broker.get_active_orders = lambda: (object(),)
        elif fault == "pending":
            broker.has_pending_critical_events = lambda: True
        else:
            broker.positions = [ContractPosition("A2701", "DCE", long_today=1, long_price=2000)]

    broker.authorize_candidate_order_requests = authorize
    result = manager.maybe_rebalance(_at(manager))
    assert result.action == "reject"
    assert len(broker.orders) == 1


@pytest.mark.parametrize("limit_up", [0, float("nan"), 1998, 3000])
def test_short_roll_requires_trusted_upper_bound_and_entire_budget(tmp_path, limit_up):
    manager, broker, _, now = _roll_manager(tmp_path, short=True)
    assert manager.maybe_rebalance(now).action == "reduce"
    broker.positions = []
    later = _at(manager)
    manager._ticks["A2701"] = replace(manager._ticks["A2701"], limit_up=limit_up)
    result = manager.maybe_rebalance(later)
    assert result.action == "reject"
    assert len(broker.orders) == 1


def test_short_improved_fill_cost_remains_consumed_after_lower_quote(tmp_path):
    manager, broker, _, now = _roll_manager(tmp_path, short=True)
    assert manager.maybe_rebalance(now).action == "reduce"
    broker.positions = []
    assert manager.maybe_rebalance(_at(manager)).action == "open"
    _fill(manager, broker, tmp_path, broker.orders[-1], volume=1, price=2200)
    # SELL fills can improve above their limit, hence the exchange limit_up reserve.
    later = _at(manager, price=1802)
    manager._ticks["A2701"] = replace(manager._ticks["A2701"], limit_up=3000)
    result = manager.maybe_rebalance(later)
    assert result.action == "reject"
    assert len(broker.orders) == 2


def test_day_end_authority_still_blocks_authenticated_later_roll(tmp_path):
    manager, broker, _, now = _roll_manager(tmp_path)
    assert manager.maybe_rebalance(now).action == "reduce"
    broker.positions = []

    def blocked(_request):
        raise RuntimeError("Stress-90 day-end opening is paused")

    manager.day_end_order_authority = blocked
    result = manager.maybe_rebalance(_at(manager))
    assert result.action == "reject" and "day-end" in result.reason
    assert len(broker.orders) == 1


def test_cancel_terminal_reports_fill_before_trade_callback_cannot_release_budget(tmp_path):
    manager, broker, _, now = _roll_manager(tmp_path)
    assert manager.maybe_rebalance(now).action == "reduce"
    broker.positions = []
    journal = _fill(manager, broker, tmp_path, _request(manager), volume=1, price=2000)
    entry = journal.load_required().all_entries[0]
    # Terminal order arrival can precede its trade callback and position mutation.
    broker.get_order_submission_identities = lambda: (
        replace(entry, filled_volume=0, fill_keys=(), fill_evidence=()),
    )
    broker.positions = []
    assert manager.maybe_rebalance(_at(manager)).action == "reject"
    assert len(broker.orders) == 1


def test_restart_restores_durable_fill_consumption_and_partial_roll_authority(tmp_path):
    from test_ctp_order_submission_boundary import _raw_trade

    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager

    manager, fake, path, now = _roll_manager(tmp_path)
    assert manager.maybe_rebalance(now).action == "reduce"
    intent = manager.execution_intent_store.load_required_record().intent
    original = manager.policy_state_store.load_required()
    journal_path = tmp_path / "ctp_orders.json"
    first_broker, first_calls = _ctp_broker(manager, fake, journal_path)
    manager.broker = first_broker
    first = manager.maybe_rebalance(_at(manager))
    assert first.action == "open", first.reason
    assert first_calls[0].volume == 2
    assert manager.maybe_rebalance(_at(manager)).action == "wait"  # submitted/unknown
    order_id = first_broker.get_order_submission_identities()[0].order_id
    first_broker._on_trade(SimpleNamespace(data=_raw_trade(order_id, "T1")))
    first_broker._pending_order_journal_status[order_id] = "terminal"
    first_broker.poll_events()
    first_broker.checkpoint_order_submission_journal()
    assert first_broker.acknowledge_critical_events()
    restored_positions = first_broker.get_positions()
    assert restored_positions[0].long_total == 1

    restarted_broker, later_calls = _ctp_broker(manager, fake, journal_path, order_ref=41)
    restarted_broker._positions = {(p.symbol, p.exchange): p for p in restored_positions}
    restarted = Stress90DirectionalPortfolioManager(
        manager.config,
        restarted_broker,
        manager.risk_manager,
        historical_mode=True,
        policy_state_path=manager.policy_state_store.path,
        seed_path=manager.seed_store.path,
        oi_evidence_path=manager.oi_evidence_store.path,
        execution_intent_path=path,
        activity_tracker=manager.activity_tracker,
        static_specs=manager._specs,
    )
    restarted._initialized = True
    restarted._catalog = manager._catalog
    restarted._catalog_by_symbol = manager._catalog_by_symbol
    restarted._ticks = manager._ticks
    restarted._prepare_decision_for_current_day = lambda current, **kwargs: (
        original.prepared_decision
    )
    result = restarted.maybe_rebalance(_at(restarted))
    assert result.action == "open", result.reason
    assert later_calls[0].volume == 1
    assert restarted.execution_intent_store.load_required_record().intent == intent
    assert restarted.policy_state_store.load_required() == original


@pytest.mark.parametrize("boundary", ["after_prepare", "official_send"])
@pytest.mark.parametrize("event_kind", ["late_fill", "cancel_terminal"])
def test_late_callback_after_roll_guard_blocks_official_ctp_send(
    tmp_path, monkeypatch, boundary, event_kind
):
    from test_ctp_order_submission_boundary import _raw_trade

    manager, fake, _, now = _roll_manager(tmp_path)
    assert manager.maybe_rebalance(now).action == "reduce"
    armed = False
    prior_id = ""

    def callback():
        if not armed:
            return
        if event_kind == "late_fill":
            broker._on_trade(SimpleNamespace(data=_raw_trade(prior_id, "T2")))
        else:
            request = broker.get_order_submission_identity(prior_id).request
            broker._convert_order = lambda raw: raw
            broker._on_order(
                SimpleNamespace(
                    data=Order(
                        prior_id,
                        request,
                        OrderStatus.CANCELLED,
                        traded=2,
                    )
                )
            )

    broker, calls = _ctp_broker(
        manager,
        fake,
        tmp_path / "ctp_orders.json",
        before_official_send=callback if boundary == "official_send" else None,
    )
    manager.broker = broker
    assert manager.maybe_rebalance(_at(manager)).action == "open"
    prior_id = broker.get_order_submission_identities()[0].order_id
    broker._on_trade(SimpleNamespace(data=_raw_trade(prior_id, "T1")))
    broker._pending_order_journal_status[prior_id] = "terminal"
    broker.poll_events()
    broker.checkpoint_order_submission_journal()
    assert broker.acknowledge_critical_events()
    armed = True
    if boundary == "after_prepare":
        native = broker._order_submission_journal.prepare

        def prepare_then_callback(entry, **kwargs):
            result = native(entry, **kwargs)
            callback()
            return result

        monkeypatch.setattr(broker._order_submission_journal, "prepare", prepare_then_callback)
    result = manager.maybe_rebalance(_at(manager))
    assert result.action == "reject" and "critical event" in result.reason
    assert len(calls) == 1  # only the first authorized order reached the synthetic gateway
    entries = broker._order_submission_journal.load_required().all_entries
    assert entries[-1].status == "aborted_before_send"
    assert broker.get_positions()[0].long_total == (2 if event_kind == "late_fill" else 1)


def test_limit_price_one_tick_over_budget_cannot_use_mid_price_cap(tmp_path):
    manager, broker, _, now = _roll_manager(tmp_path, target_weight=0.6)
    assert manager.maybe_rebalance(now).action == "reduce"
    broker.positions = []
    _fill(manager, broker, tmp_path, _request(manager, price=3000), volume=1, price=3000)
    later = _at(manager, price=1000)
    # Durable consumed=30k; two requested lots at mid fit exactly, BUY limit does not.
    assert _windows(manager, _request(manager, volume=2, price=1001), later) == (
        PRODUCT_SESSION_MANIFEST["A"].first_entry_window,
    )
    assert manager.maybe_rebalance(later).action == "reject"


def test_already_held_target_cannot_become_later_session_replacement(tmp_path):
    manager, broker, _, now = _roll_manager(tmp_path)
    broker.positions.append(ContractPosition("A2701", "DCE", long_today=1, long_price=2000))
    assert manager.maybe_rebalance(now).action == "reduce"
    transition = manager.execution_intent_store.load_required_record().intent.transitions[0]
    assert transition.kind == "same_product_roll" and "A2701" in transition.source_symbols
    broker.positions = [broker.positions[-1]]
    assert manager.maybe_rebalance(_at(manager)).action == "reject"
    assert len(broker.orders) == 1


def test_first_window_authorization_cannot_carry_oversized_roll_into_later_time(tmp_path):
    from datetime import timedelta

    manager, broker, _, now = _roll_manager(tmp_path, target_weight=2.0)
    assert manager.maybe_rebalance(now).action == "reduce"
    broker.positions = []
    wall = [now.replace(minute=9, second=59)]
    manager._ticks = {
        symbol: replace(tick, timestamp=wall[0]) for symbol, tick in manager._ticks.items()
    }
    manager.historical_mode = False
    manager.health_clock = lambda: wall[0]
    manager.elapsed_clock = lambda: 100.0
    broker.health_error = lambda: None
    broker.authorize_candidate_order_requests = lambda _requests: wall.__setitem__(
        0, wall[0] + timedelta(seconds=2)
    )
    result = manager.maybe_rebalance(wall[0])
    assert result.action == "reject" and "session" in result.reason
    assert len(broker.orders) == 1
