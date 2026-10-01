from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_stress90_lifecycle_transaction import _stores
from test_stress90_operator_roll_forward import _market_stores

from afuture.models import AccountSnapshot


def test_one_runtime_rolls_across_month_and_holiday_without_reset(tmp_path: Path) -> None:
    """Synthetic account evidence exercises persistent transaction state, not CTP finality."""
    from afuture.directional_activity import (
        ContractActivity,
        DirectionalActivitySnapshot,
        DirectionalActivityStore,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_state import (
        Stress90DecisionInputs,
        prepare_stress90_decision,
    )
    from afuture.runtime_calendar import RuntimeTradingCalendar
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        apply_stress90_lifecycle_transaction,
        build_stress90_settlement_roll_forward_targets,
    )
    from afuture.stress90_operator_continuity import (
        Stress90OperatorContinuityError,
        build_stress90_operator_roll_forward_plan,
        load_stress90_operator_account_day_continuity_evidence,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    holding = {
        "symbol": "A2612",
        "exchange": "DCE",
        "long_today": 0,
        "long_yesterday": 2,
        "short_today": 0,
        "short_yesterday": 0,
        "long_price": 1000.0,
        "short_price": 0.0,
    }
    generic = generic_store.save(
        replace(
            generic.state,
            positions=[holding],
            last_trade_id="synthetic-fill-1",
            recent_trade_ids=["synthetic-fill-1"],
        ),
        expected_sequence=generic.sequence,
    )
    policy = policy_store.save(
        replace(policy.state, live_inception_day="20260825", live_inception_equity=700_000.0),
        expected_sequence=policy.sequence,
    )

    def prepare_target(previous_day: str, target_day: str, ohlc_store, oi_store) -> None:
        completed = oi_store.load_required_record().state.completed[0]
        assert completed.trading_day == previous_day and completed.complete
        close = ohlc_store.load(STRESS90_POLICY.products).close
        prepared = prepare_stress90_decision(
            policy_store,
            Stress90DecisionInputs(
                previous_target_trading_day=previous_day,
                target_trading_day=target_day,
                base_weights=dict.fromkeys(STRESS90_POLICY.products, 0.0),
                completed_close_history={
                    product: tuple(float(value) for value in close[product])
                    for product in STRESS90_POLICY.products
                },
                completed_oi_flow=completed.flows,
                completed_close_day=previous_day,
                completed_oi_day=previous_day,
            ),
        )
        assert prepared.target_trading_day == target_day

    initial_ohlc, initial_oi = _market_stores(source_day="20260824", target_day="20260825")
    prepare_target("20260824", "20260825", initial_ohlc, initial_oi)
    policy = policy_store.load_required_record()
    calendar = RuntimeTradingCalendar.load()
    store = Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
    activity_store = DirectionalActivityStore(tmp_path / "directional_activity.json")
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
        activity_store.save(
            DirectionalActivitySnapshot(
                source_day,
                {
                    "A2612": ContractActivity(
                        "A2612",
                        "DCE",
                        "A",
                        source_day,
                        20_000.0,
                        30_000.0,
                        datetime.strptime(source_day, "%Y%m%d").replace(tzinfo=timezone.utc),
                    )
                },
            )
        )
        completed_activity = activity_store.load()
        assert completed_activity is not None and completed_activity.trading_day == source_day
        equity = 699_000.0 if index == 11 else 700_000.0 + index * 100.0
        account = AccountSnapshot(
            balance=equity,
            equity=equity,
            available=equity,
            margin=0.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            trading_day=target_day,
            previous_settlement_equity=equity,
            settlement_verified=True,
            settlement_id=43 + index,
        )
        if index == 9:
            with pytest.raises(Stress90LifecycleTransactionError):
                build_stress90_settlement_roll_forward_targets(
                    generic.state,
                    policy.state,
                    replace(account, settlement_verified=False),
                )
            with pytest.raises(Stress90LifecycleTransactionError):
                build_stress90_settlement_roll_forward_targets(
                    generic.state,
                    policy.state,
                    replace(account, deposit=1.0),
                )
            assert generic_store.load_required_record() == generic
        runtime = tmp_path.resolve()
        runtime_digest = sha256(f"runtime:{runtime}".encode()).hexdigest()
        binding = SimpleNamespace(
            account_identity_digest="b" * 64,
            account_epoch=policy.state.live_account_epoch,
            canonical_runtime=str(runtime),
            runtime_identity_digest=runtime_digest,
        )
        registry = SimpleNamespace(
            binding=binding,
            binding_receipt_digest="9" * 64,
            registry_sequence=index,
            registry_checksum="8" * 64,
        )
        trading_day_evidence = SimpleNamespace(
            phase="bound",
            trading_day=source_day,
            account_identity_digest="b" * 64,
            account_epoch=policy.state.live_account_epoch,
            canonical_runtime=str(runtime),
            runtime_identity_digest=runtime_digest,
            account_binding_receipt_digest=registry.binding_receipt_digest,
            registry_sequence=registry.registry_sequence,
            registry_checksum=registry.registry_checksum,
            sequence=index,
            checksum="e" * 64,
        )
        plan_inputs = dict(
            operation_id=f"{index:064x}",
            operator_reason="synthetic offline continuity",
            generic_source=generic,
            policy_source=policy,
            source_trading_day_evidence=trading_day_evidence,
            source_registry_evidence=registry,
            runtime_dir=runtime,
            account_identity_digest="b" * 64,
            account_snapshot=account,
            active_order_count=0,
            positions_reconciled=True,
            position_reconciliation_digest="1" * 64,
            session_ownership_digest="2" * 64,
            ctp_order_journal_digest="3" * 64,
            activity_latest_completed_day=completed_activity.trading_day,
            market_continuity=market,
        )
        if index == 9:
            with pytest.raises(Stress90OperatorContinuityError, match="zero active orders"):
                build_stress90_operator_roll_forward_plan(
                    **{**plan_inputs, "active_order_count": 1}
                )
            with pytest.raises(Stress90OperatorContinuityError, match="position reconciliation"):
                build_stress90_operator_roll_forward_plan(
                    **{**plan_inputs, "positions_reconciled": False}
                )
            assert policy_store.load_required_record() == policy
        plan = build_stress90_operator_roll_forward_plan(**plan_inputs)
        targets = plan.targets
        assert targets == build_stress90_settlement_roll_forward_targets(
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
            account_day_continuity_digest=plan.request_digest,
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
        assert generic.state.positions == [holding]
        assert generic.state.last_trade_id == "synthetic-fill-1"
        assert generic.state.recent_trade_ids == ["synthetic-fill-1"]
        assert generic.state.equity_high_watermark >= 700_000.0
        assert policy.state.live_inception_equity == 700_000.0
        assert policy.state.completed_account_wealth == pytest.approx(
            account.previous_settlement_equity / 700_000.0
        )
        if index == 12:
            with pytest.raises(Stress90LifecycleTransactionError, match="advance"):
                build_stress90_settlement_roll_forward_targets(
                    generic.state,
                    policy.state,
                    replace(account, settlement_id=account.settlement_id + 1),
                )
            assert policy_store.load_required_record() == policy
        if index == 11:
            assert generic.state.equity_high_watermark == 701_000.0
            assert policy.state.completed_account_high_watermark == pytest.approx(
                701_000.0 / 700_000.0
            )
        prepare_target(source_day, target_day, ohlc_store, oi_store)
        policy = policy_store.load_required_record()
        assert policy.state.last_completed_target_day == generic.state.trading_day
    assert generic.state.trading_day == "20260928"
    assert policy.state.last_completed_account_day == days[-2].strftime("%Y%m%d")
    assert generic.state.equity_high_watermark == 700_000.0 + len(days) * 100.0
