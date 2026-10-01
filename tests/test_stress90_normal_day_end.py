from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep

import pytest
from stress90_offline_counter import ACCOUNT, CHINA, EPOCH, OfflineMonth

from afuture.models import RuntimeMode
from afuture.reconcile import compare_positions


def _drain(fixture, predicate, *, limit=5.0):
    deadline = monotonic() + limit
    while monotonic() < deadline:
        fixture.engine.run_once()
        assert not fixture.engine.halted, fixture.engine.state.kill_reason
        if predicate():
            return
        sleep(0.001)
    pytest.fail("resident loop did not finish: " + fixture.engine.day_end_coordinator.last_error)


def _complete_day(fixture, day):
    if fixture.now.hour == 21:
        fixture.emit(day, fixture.now.replace(minute=1), volume=1200, hold=30_005)
        fixture.engine.run_once()
    date = datetime.strptime(day, "%Y%m%d").replace(tzinfo=CHINA)
    for hour, minute, vol, hold in (
        (9, 0, 2000, 30_010),
        (9, 1, 2200, 30_020),
        (14, 59, 20000, 30_100),
    ):
        # Fixed alternating close and rising OI exercise the frozen confirmation
        # rule causally: the next session closes/re-enters C without target edits.
        shift = -0.5 if hour == 14 and int(day[-2:]) % 2 else 0.5
        if date.replace(hour=hour, minute=minute) <= fixture.now:
            continue
        fixture.emit(
            day, date.replace(hour=hour, minute=minute), volume=vol, hold=hold, shift=shift
        )
        fixture.engine.run_once()
        assert not fixture.engine.halted, fixture.engine.state.kill_reason
    assert not fixture.broker.get_active_orders()
    fixture.save_ohlc(day)


def _advance(fixture, source, target, *, restart=False):
    fixture.broker.synchronize_trading_day(target)  # external counter input, not runtime WAL
    fixture.engine.run_once()
    assert not fixture.engine.halted, fixture.engine.state.kill_reason
    assert fixture.engine.day_end_paused, (
        source,
        target,
        fixture.engine._initialized,
        fixture.broker.is_ready(),
        fixture.engine.state.trading_day,
        fixture.engine.state.runtime_mode,
        fixture.engine.day_end_coordinator.last_error,
        [event.event_type for event in fixture.broker._events],
    )
    # Wait only for the background catalog query before publishing raw new-session packets.
    coordinator = fixture.engine.day_end_coordinator
    deadline = monotonic() + 5
    while (coordinator._query is None or not coordinator._query.done()) and monotonic() < deadline:
        sleep(0.001)
    assert coordinator._query is not None and coordinator._query.done()
    if restart:
        root = fixture.root
        fixture.close()
        fixture = OfflineMonth(root, restore=True)
        fixture.engine.run_once()
        coordinator = fixture.engine.day_end_coordinator
        deadline = monotonic() + 5
        while (
            coordinator._query is None or not coordinator._query.done()
        ) and monotonic() < deadline:
            sleep(0.001)
        assert coordinator._query is not None and coordinator._query.done()
    opening = fixture.session_open(target)
    fixture.emit(target, opening, volume=1000, hold=30_000)
    _drain(fixture, lambda: not fixture.engine.day_end_paused)
    assert fixture.engine.state.trading_day == target
    assert fixture.engine.state.day_start_equity == pytest.approx(
        fixture.broker.get_account().previous_settlement_equity
    )
    assert compare_positions(
        fixture.engine.state_store.positions_from_state(fixture.engine.state),
        fixture.broker.get_positions(),
    ).matched
    return fixture


def test_two_normal_days_use_one_authorization_and_trade_again(tmp_path: Path):
    fixture = OfflineMonth(tmp_path)
    try:
        fixture.engine.run_once()
        _complete_day(fixture, "20260825")
        first_positions = fixture.broker.get_positions()
        first_ids = set(fixture.engine.state.recent_trade_ids)
        assert first_positions and first_ids
        permit = fixture.engine.technical_activation_authority.permit_store.load_required_record()
        # A fixed synthetic price move exercises integer re-sizing. The account
        # marks it from actual fills; strategy, targets and equity are untouched.
        fixture.market_prices[first_positions[0].symbol] = 1010.0
        _advance(fixture, "20260825", "20260826")
        assert all(p.long_today == p.short_today == 0 for p in fixture.broker.get_positions())
        _complete_day(fixture, "20260826")
        assert set(fixture.engine.state.recent_trade_ids) > first_ids
        assert fixture.engine.state.runtime_mode == RuntimeMode.RUNNING.value
        assert not fixture.engine.state.kill_switch
        assert (
            fixture.engine.technical_activation_authority.permit_store.load_required_record()
            == permit
        )
        policy = fixture.engine.directional_manager.policy_state_store.load_required()
        assert policy.live_account_epoch == EPOCH and policy.live_account_identity_digest == ACCOUNT
        assert policy.last_completed_account_day == "20260825"
    finally:
        fixture.close()


def test_same_account_month_reconciles_every_day_and_restarts(tmp_path: Path):
    fixture = OfflineMonth(tmp_path)
    rows = []
    try:
        fixture.engine.run_once()
        permit = fixture.engine.technical_activation_authority.permit_store.load_required_record()
        days = [
            d.strftime("%Y%m%d")
            for d in fixture.calendar.open_days["DCE"]
            if "20260825" <= d.strftime("%Y%m%d") <= "20260928"
        ]
        assert days[0] == "20260825" and days[-1] == "20260928"
        trading_days = set()
        for index, day in enumerate(days):
            if index:
                if index == 1:
                    fixture.market_prices[fixture.broker.get_positions()[0].symbol] = 1010.0
                fixture = _advance(fixture, days[index - 1], day, restart=index in (6, 20))
            _complete_day(fixture, day)
            account = fixture.broker.get_account()
            state = fixture.engine.state
            policy = fixture.engine.directional_manager.policy_state_store.load_required()
            assert compare_positions(
                fixture.engine.state_store.positions_from_state(state),
                fixture.broker.get_positions(),
            ).matched
            assert account.deposit == account.withdrawal == 0
            assert state.equity_high_watermark >= account.equity
            assert policy.live_account_epoch == EPOCH
            assert policy.live_account_identity_digest == ACCOUNT
            assert policy.live_inception_equity == 100_000.0
            assert (
                fixture.engine.technical_activation_authority.permit_store.load_required_record()
                == permit
            )
            if fixture.broker.get_trades():
                trading_days.add(day)
            rows.append(
                {
                    "day": day,
                    "account": asdict(account),
                    "day_start_equity": state.day_start_equity,
                    "high_watermark": state.equity_high_watermark,
                    "orders": len(fixture.broker.get_orders()),
                    "fills": len(fixture.broker.get_trades()),
                    "fees": sum(t.commission for t in fixture.broker.get_trades()),
                    "positions": [asdict(p) for p in fixture.broker.get_positions()],
                    "policy_account_day": policy.last_completed_account_day,
                    "trade_ids": list(state.recent_trade_ids),
                }
            )
        assert len(trading_days) >= 2
        assert sum(r["fees"] for r in rows) > 0
        assert len(days) >= 20
        assert "20260925" not in days  # Mid-Autumn holiday; next open is Sep 28.
        fixture = _advance(fixture, days[-1], "20260929")
        assert (
            fixture.engine.directional_manager.policy_state_store.load_required().last_completed_account_day
            == days[-1]
        )
        assert fixture.engine.state.day_start_equity == pytest.approx(rows[-1]["account"]["equity"])
        (tmp_path / "daily-evidence.json").write_text(json.dumps(rows, sort_keys=True))
    finally:
        fixture.close()
