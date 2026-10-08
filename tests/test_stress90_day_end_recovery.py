from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep

import pytest
from stress90_offline_counter import CHINA, OfflineMonth
from test_stress90_normal_day_end import _advance, _complete_day, _drain

from afuture.models import (
    BrokerEvent,
    Offset,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    RuntimeMode,
)


def _first_close(fixture):
    fixture.engine.run_once()
    _complete_day(fixture, "20260825")


def _pause(fixture, *, emit=True):
    fixture.broker.synchronize_trading_day("20260826")
    fixture.engine.run_once()
    assert not fixture.engine.halted, fixture.engine.state.kill_reason
    coordinator = fixture.engine.day_end_coordinator
    deadline = monotonic() + 5
    while (coordinator._query is None or not coordinator._query.done()) and monotonic() < deadline:
        sleep(0.001)
    assert coordinator._query is not None and coordinator._query.done()
    if emit:
        fixture.emit("20260826", fixture.session_open("20260826"), volume=1000, hold=30_000)
    return coordinator


def test_missing_settlement_waits_and_restart_uses_initial_authorization(tmp_path: Path):
    fixture = OfflineMonth(tmp_path)
    try:
        _first_close(fixture)
        permit = fixture.engine.technical_activation_authority.permit_store.load_required_record()
        fixture.broker.stress90_settlement_provider.missing = True
        _pause(fixture, emit=False)
        for _ in range(3):
            fixture.engine.run_once()
        assert fixture.engine.day_end_paused and not fixture.engine.halted
        assert fixture.engine.state.trading_day == "20260825"
        assert fixture.broker.get_orders() == []
        fixture.close()
        fixture = OfflineMonth(tmp_path, restore=True)
        fixture.engine.run_once()
        coordinator = fixture.engine.day_end_coordinator
        deadline = monotonic() + 5
        while (
            coordinator._query is None or not coordinator._query.done()
        ) and monotonic() < deadline:
            sleep(0.001)
        fixture.emit("20260826", fixture.session_open("20260826"), volume=1000, hold=30_000)
        _drain(fixture, lambda: not fixture.engine.day_end_paused)
        assert (
            fixture.engine.technical_activation_authority.permit_store.load_required_record()
            == permit
        )
    finally:
        fixture.close()


class ProcessExit(BaseException):
    pass


@pytest.mark.parametrize(
    "point", ["before_prepare", "after_policy", "after_generic", "after_commit"]
)
def test_day_end_crash_recovers_exact_transaction_once(tmp_path: Path, monkeypatch, point):
    fixture = OfflineMonth(tmp_path)
    try:
        _first_close(fixture)
        permit = fixture.engine.technical_activation_authority.permit_store.load_required_record()
        identities = list(fixture.engine.state.recent_trade_ids)
        coordinator = _pause(fixture)
        if point == "before_prepare":
            owner, method = coordinator.transactions, "begin"
        elif point == "after_policy":
            owner, method = fixture.engine.directional_manager.policy_state_store, "save"
        elif point == "after_generic":
            owner, method = fixture.engine.state_store, "save"
        else:
            owner, method = coordinator.transactions, "mark_committed"
        original = getattr(owner, method)

        def crash(*args, **kwargs):
            if point == "after_generic" and args[0].trading_day != "20260826":
                return original(*args, **kwargs)
            if point != "before_prepare":
                original(*args, **kwargs)
            raise ProcessExit(point)

        with monkeypatch.context() as fault:
            # Inject only a persistence failure; all identity, risk and evidence
            # validators run unchanged before reaching the fault.
            fault.setattr(owner, method, crash)
            with pytest.raises(ProcessExit):
                fixture.engine.run_once()
        fixture.close()
        fixture = OfflineMonth(tmp_path, restore=True)
        fixture.emit(
            "20260826", fixture.session_open("20260826").replace(minute=2), volume=1200, hold=30_005
        )
        _drain(fixture, lambda: not fixture.engine.day_end_paused)
        assert fixture.engine.state.recent_trade_ids == identities
        assert (
            fixture.engine.state.day_start_equity
            == fixture.broker.get_account().previous_settlement_equity
        )
        assert (
            fixture.engine.directional_manager.policy_state_store.load_required().last_completed_account_day
            == "20260825"
        )
        assert (
            fixture.engine.technical_activation_authority.permit_store.load_required_record()
            == permit
        )
        committed = coordinator.transactions.load_required()
        assert committed.status == "committed"
        for _ in range(2):
            fixture.engine.run_once(allow_strategy=False)
        assert coordinator.transactions.load_required() == committed
    finally:
        fixture.close()


@pytest.mark.parametrize("kind", ["unknown_order", "duplicate_fill", "missing_fill"])
def test_incomplete_or_foreign_settlement_never_commits(tmp_path: Path, kind):
    fixture = OfflineMonth(tmp_path)
    try:
        _first_close(fixture)
        fixture.broker.synchronize_trading_day("20260826")
        provider = fixture.broker.stress90_settlement_provider

        def change(raw):
            if kind == "unknown_order":
                raw["orders"].append(
                    asdict(
                        Order(
                            "EXTERNAL-1",
                            OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 1000),
                            OrderStatus.CANCELLED,
                        )
                    )
                )
            elif kind == "duplicate_fill":
                raw["trades"].append(raw["trades"][0])
            else:
                raw["trades"].pop()

        provider.publish_correction("20260825", "20260826", change)
        _pause(fixture, emit=False)
        fixture.emit("20260826", fixture.session_open("20260826"), volume=1000, hold=30_000)
        fixture.engine.run_once()
        assert fixture.engine.halted
        assert fixture.engine.state.trading_day == "20260825"
        assert fixture.engine.day_end_coordinator.transactions.load() is None
        assert any(
            message in fixture.engine.state.kill_reason
            for message in (
                "unknown",
                "duplicate",
                "remaining quantity",
                "fees",
                "consumed",
            )
        )
    finally:
        fixture.close()


def test_duplicate_and_late_callbacks_do_not_double_book_fills(tmp_path: Path):
    fixture = OfflineMonth(tmp_path)
    try:
        _first_close(fixture)
        trade = fixture.broker.get_trades()[0]
        order = fixture.broker.get_orders()[0]
        positions = fixture.engine.state.positions
        identities = list(fixture.engine.state.recent_trade_ids)
        fixture.broker._events.extend(
            [BrokerEvent("trade", trade), BrokerEvent("trade", trade), BrokerEvent("order", order)]
        )
        fixture.engine.run_once()
        assert fixture.engine.state.positions == positions
        assert fixture.engine.state.recent_trade_ids == identities
        _advance(fixture, "20260825", "20260826")
        fixture.broker._events.extend([BrokerEvent("trade", trade), BrokerEvent("order", order)])
        fixture.engine.run_once(allow_strategy=False)
        assert not fixture.engine.halted
        assert fixture.engine.state.recent_trade_ids == identities
        fixture.broker._events.append(BrokerEvent("trade", replace(trade, trade_id="UNSEEN-LATE")))
        fixture.engine.run_once()
        assert fixture.engine.halted
        assert fixture.engine.state.recent_trade_ids == identities
    finally:
        fixture.close()


@pytest.mark.parametrize(
    "kind", ["manual", "hard_risk", "expired", "illegal_session", "provider_missing"]
)
def test_stops_and_unqualified_sessions_cannot_resume(tmp_path: Path, kind):
    expiry = "2026-08-26T08:00:00+08:00" if kind == "expired" else "2026-10-10T00:00:00+08:00"
    fixture = OfflineMonth(tmp_path, valid_until=expiry)
    try:
        _first_close(fixture)
        if kind == "illegal_session":
            fixture.broker.synchronize_trading_day("20260828")
        else:
            _pause(fixture, emit=False)
        if kind == "manual":
            fixture.engine.emergency_stop("operator stop during day-end")
        elif kind == "hard_risk":
            fixture.market_prices = {s: 900.0 for s in fixture.market_prices}
            fixture.emit("20260826", fixture.session_open("20260826"), volume=1000, hold=30_000)
        elif kind == "expired":
            fixture.now = datetime(2026, 8, 26, 9, tzinfo=CHINA)
        elif kind == "provider_missing":
            fixture.engine.day_end_coordinator.provider = None
        for _ in range(3):
            fixture.engine.run_once()
        assert fixture.engine.state.runtime_mode != RuntimeMode.RUNNING.value
        assert fixture.engine.state.trading_day == "20260825"
        assert fixture.engine.day_end_coordinator.transactions.load() is None
        assert not any(o.request.offset is Offset.OPEN for o in fixture.broker.get_orders())
    finally:
        fixture.close()


def test_commit_then_correction_keeps_original_and_stops(tmp_path: Path):
    fixture = OfflineMonth(tmp_path)
    try:
        _first_close(fixture)
        _advance(fixture, "20260825", "20260826")
        provider = fixture.broker.stress90_settlement_provider
        original = (provider.root / "settlement-20260825-20260826.json").read_bytes()
        provider.publish_correction("20260825", "20260826", lambda raw: raw.update(version=2))
        fixture.engine.run_once()
        assert fixture.engine.halted
        assert (provider.root / "settlement-20260825-20260826.json").read_bytes() == original
        assert "corrected" in fixture.engine.state.kill_reason
    finally:
        fixture.close()
