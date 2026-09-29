from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_stress90_lifecycle_transaction import _stores
from test_stress90_operator_roll_forward import _market_stores

from afuture.models import AccountSnapshot


def test_one_runtime_rolls_across_month_and_holiday_without_reset(tmp_path: Path) -> None:
    """Synthetic account evidence exercises persistent transaction state, not CTP finality."""
    from afuture.runtime_calendar import RuntimeTradingCalendar
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionStore,
        apply_stress90_lifecycle_transaction,
        build_stress90_settlement_roll_forward_targets,
    )
    from afuture.stress90_operator_continuity import (
        load_stress90_operator_account_day_continuity_evidence,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    policy = policy_store.save(
        replace(policy.state, live_inception_day="20260825", live_inception_equity=700_000.0),
        expected_sequence=policy.sequence,
    )
    calendar = RuntimeTradingCalendar.load()
    store = Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
    days = [
        day for day in calendar.open_days["SHFE"] if "2026-08-25" < day.isoformat() <= "2026-09-28"
    ]
    assert len(days) >= 20
    assert any(day.month == 9 for day in days)
    assert any((right - left).days > 1 for left, right in zip(days, days[1:], strict=False))
    for index, day in enumerate(days, 1):
        source_day = generic.state.trading_day
        target_day = day.strftime("%Y%m%d")
        ohlc_store, oi_store = _market_stores(source_day=source_day, target_day=target_day)
        market = load_stress90_operator_account_day_continuity_evidence(
            ohlc_store,
            oi_store,
            completed_account_day=source_day,
            current_ctp_trading_day=target_day,
        )
        account = AccountSnapshot(
            balance=700_000.0 + index * 100.0,
            equity=700_000.0 + index * 100.0,
            available=700_000.0 + index * 100.0,
            margin=0.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            trading_day=target_day,
            previous_settlement_equity=700_000.0 + index * 100.0,
            settlement_verified=True,
            settlement_id=43 + index,
        )
        targets = build_stress90_settlement_roll_forward_targets(
            generic.state, policy.state, account
        )
        transaction = store.begin(
            operation="settlement_roll_forward",
            generic_source=generic,
            policy_source=policy,
            generic_target=targets.generic_target,
            policy_target=targets.policy_target,
            trading_day=account.trading_day,
            account_identity_digest="b" * 64,
            account_snapshot=account,
            account_day_continuity_digest=market.continuity_digest,
            operation_nonce=f"{index:064x}",
            operator_reason="synthetic offline continuity",
        )
        if index == 8:
            original_mark = store.mark_committed

            def interrupted_commit(*_args, **_kwargs):
                raise RuntimeError("synthetic crash before commit marker")

            store.mark_committed = interrupted_commit
            with pytest.raises(RuntimeError, match="synthetic crash"):
                apply_stress90_lifecycle_transaction(
                    store, generic_store=generic_store, policy_store=policy_store
                )
            store.mark_committed = original_mark
            store = Stress90LifecycleTransactionStore(store.path)
        assert (
            apply_stress90_lifecycle_transaction(
                store, generic_store=generic_store, policy_store=policy_store
            ).status
            == "committed"
        )
        assert store.load_required().transaction_id == transaction.transaction_id
        generic, policy = generic_store.load_required_record(), policy_store.load_required_record()
        previous_sequence = policy.sequence
        assert (
            apply_stress90_lifecycle_transaction(
                store, generic_store=generic_store, policy_store=policy_store
            ).status
            == "committed"
        )
        assert policy_store.load_required_record().sequence == previous_sequence
        assert generic.state.runtime_mode == "HALTED"
        assert generic.state.kill_switch is True
        assert generic.state.equity_high_watermark >= 700_000.0
        assert policy.state.live_inception_equity == 700_000.0
    assert generic.state.trading_day == "20260928"
    assert policy.state.last_completed_account_day == days[-2].strftime("%Y%m%d")
