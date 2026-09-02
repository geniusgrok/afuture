from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from afuture.models import AccountSnapshot, RuntimeMode
from afuture.state import RuntimeState, StateStore

_SOURCE_ACCOUNT_EPOCH = "c" * 64
_OPERATION_NONCE = "d" * 64


def _stores(runtime_dir: Path):
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        bind_stress90_account_identity,
    )

    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260824",
        last_decision_digest="a" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "1" * 64},
        bootstrap_through_day="20260824",
        last_completed_input_day="20260821",
    )
    generic_store = StateStore(runtime_dir / "state.json")
    policy_store = Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json")
    generic = generic_store.save(
        activate_stress90_policy(
            RuntimeState(
                kill_switch=True,
                runtime_mode=RuntimeMode.HALTED.value,
                reconciled=True,
                trading_day="20260825",
                day_start_equity=700_000.0,
                equity_high_watermark=700_000.0,
                last_account_equity=700_000.0,
                last_account_trading_day="20260825",
                last_account_cash_flow_verified=True,
                last_account_settlement_id=43,
            ),
            broker_flat=True,
            local_flat=True,
            no_active_orders=True,
            reconciled=True,
            bootstrap_seed_digest=seed.seed_digest,
            account_identity_digest="b" * 64,
            risk_overlay_digest="e" * 64,
            operator_reason="lifecycle transaction fixture",
            strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
        )
    )
    policy = policy_store.save(
        bind_stress90_account_identity(
            Stress90PolicyState.from_seed(seed),
            "b" * 64,
            account_epoch=_SOURCE_ACCOUNT_EPOCH,
        )
    )
    return generic_store, policy_store, generic, policy


def _targets(generic, policy):
    from afuture.stress90_lifecycle_transaction import derive_stress90_account_epoch

    generic_target = replace(
        generic.state,
        kill_reason="lifecycle target",
        day_start_equity=699_000.0,
        last_account_withdrawal=1_000.0,
        directional_daily_circuit_day="",
        strategy_states={
            "directional_policy_identity": dict(
                generic.state.strategy_states["directional_policy_identity"]
            )
        },
    )
    policy_target = replace(
        policy.state,
        live_account_identity_digest="b" * 64,
        live_account_epoch=derive_stress90_account_epoch(
            operation="account_rebase",
            operation_nonce=_OPERATION_NONCE,
            policy_source_checksum=policy.checksum,
            account_identity_digest="b" * 64,
            trading_day="20260825",
        ),
        live_inception_day="20260825",
        live_inception_equity=700_000.0,
    )
    return generic_target, policy_target


def _account():
    from afuture.models import AccountSnapshot

    return AccountSnapshot(
        700_000.0,
        700_000.0,
        700_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
        withdrawal=1_000.0,
        previous_settlement_equity=700_000.0,
        settlement_verified=True,
        settlement_id=43,
    )


def test_lifecycle_transaction_resumes_after_policy_write_before_generic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionStore,
        apply_stress90_lifecycle_transaction,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    transaction_store = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    )
    transaction_store.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )
    original = policy_store.save

    def save_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash after policy replace")

    monkeypatch.setattr(policy_store, "save", save_then_crash)
    with pytest.raises(RuntimeError, match="crash after policy replace"):
        apply_stress90_lifecycle_transaction(
            transaction_store,
            generic_store=generic_store,
            policy_store=policy_store,
        )

    assert generic_store.load_required_record().checksum == generic.checksum
    assert policy_store.load_required() == policy_target
    assert transaction_store.load_required().status == "prepared"

    monkeypatch.setattr(policy_store, "save", original)
    completed = apply_stress90_lifecycle_transaction(
        transaction_store,
        generic_store=generic_store,
        policy_store=policy_store,
    )

    assert completed.status == "committed"
    assert generic_store.load() == generic_target
    assert policy_store.load_required() == policy_target


def test_account_rebase_clears_generic_adaptive_margin_returns(tmp_path: Path) -> None:
    from afuture.stress90_lifecycle_transaction import Stress90LifecycleTransactionStore

    generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic = generic_store.save(
        replace(generic.state, recent_daily_returns=[-0.08, 0.03]),
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )
    generic_target, policy_target = _targets(generic, policy)
    generic_target = replace(generic_target, recent_daily_returns=[])

    transaction = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    ).begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )

    assert transaction.generic_target.recent_daily_returns == []


def test_account_rebase_rejects_retained_generic_adaptive_margin_returns(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic = generic_store.save(
        replace(generic.state, recent_daily_returns=[-0.08, 0.03]),
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )
    generic_target, policy_target = _targets(generic, policy)

    with pytest.raises(Stress90LifecycleTransactionError, match="adaptive-margin"):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="account_rebase",
            generic_source=generic,
            policy_source=policy,
            generic_target=generic_target,
            policy_target=policy_target,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=_account(),
            operation_nonce=_OPERATION_NONCE,
        )


def _settlement_roll_forward_targets(generic, policy, *, include_completed_day: bool):
    generic_target = replace(
        generic.state,
        trading_day="20260826",
        day_start_equity=630_000.0,
        last_account_equity=625_000.0,
        last_account_trading_day="20260826",
        last_account_deposit=0.0,
        last_account_withdrawal=0.0,
        last_account_cash_flow_verified=True,
        last_account_settlement_id=44,
        recent_daily_returns=(
            [*generic.state.recent_daily_returns[-1:], -0.09999999999999998]
            if include_completed_day
            else list(generic.state.recent_daily_returns)
        ),
    )
    policy_target = policy.state
    if include_completed_day:
        policy_target = replace(
            policy.state,
            completed_account_wealth=0.9,
            completed_account_high_watermark=1.0,
            last_completed_account_day="20260825",
            recent_daily_returns_for_adaptive_margin=(-0.09999999999999998,),
        )
    return generic_target, policy_target


def _settlement_account(*, deposit: float = 0.0, withdrawal: float = 0.0):
    return AccountSnapshot(
        625_000.0,
        625_000.0,
        625_000.0,
        0.0,
        0.0,
        0.0,
        "20260826",
        deposit=deposit,
        withdrawal=withdrawal,
        previous_settlement_equity=630_000.0,
        settlement_verified=True,
        settlement_id=44,
    )


def test_settlement_roll_forward_records_inception_loss_for_drawdown_reserve(
    tmp_path: Path,
) -> None:
    from afuture.directional_stress90_state import (
        REBASE_CONFIRMATION,
        drawdown_reserve_triggered_from_state,
        rebase_stress90_account,
    )
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionStore,
        build_stress90_settlement_roll_forward_targets,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    inception, _audit = rebase_stress90_account(
        policy.state,
        account_trading_day="20260825",
        account_equity=700_000.0,
        operator_reason="bind exact live inception equity",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        strong_confirmation=REBASE_CONFIRMATION,
    )
    policy = policy_store.save(
        inception,
        expected_sequence=policy.sequence,
    )
    account = AccountSnapshot(
        510_000.0,
        510_000.0,
        510_000.0,
        0.0,
        0.0,
        0.0,
        "20260826",
        previous_settlement_equity=518_000.0,
        settlement_verified=True,
        settlement_id=44,
    )
    targets = build_stress90_settlement_roll_forward_targets(
        generic.state,
        policy.state,
        account,
    )

    transaction = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    ).begin(
        operation="settlement_roll_forward",
        generic_source=generic,
        policy_source=policy,
        generic_target=targets.generic_target,
        policy_target=targets.policy_target,
        trading_day="20260826",
        account_identity_digest="b" * 64,
        account_snapshot=account,
        account_day_continuity_digest="e" * 64,
        operation_nonce=_OPERATION_NONCE,
        operator_reason="advance one verified zero-order CTP settlement",
    )

    assert transaction.status == "prepared"
    assert transaction.account_day_continuity_digest == "e" * 64
    assert transaction.account_day_continuity_source_day == "20260825"
    assert transaction.policy_target.completed_account_wealth == pytest.approx(0.74)
    assert transaction.policy_target.last_completed_account_day == "20260825"
    assert drawdown_reserve_triggered_from_state(transaction.policy_target) is True
    assert transaction.policy_target.recent_daily_returns_for_adaptive_margin == ()
    assert transaction.generic_target.recent_daily_returns == []


def test_settlement_roll_forward_records_next_fully_owned_completed_day(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionStore,
        build_stress90_settlement_roll_forward_targets,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    policy = policy_store.save(
        replace(
            policy.state,
            live_inception_day="20260824",
            live_inception_equity=700_000.0,
        ),
        expected_sequence=policy.sequence,
    )
    generic_target, policy_target = _settlement_roll_forward_targets(
        generic,
        policy,
        include_completed_day=True,
    )
    built = build_stress90_settlement_roll_forward_targets(
        generic.state,
        policy.state,
        _settlement_account(),
    )
    assert built.completed_account_day == "20260825"
    assert built.current_ctp_trading_day == "20260826"
    assert built.completed_return == -0.09999999999999998
    assert built.partial_inception_day is False
    assert built.generic_target.day_start_equity == 630_000.0
    assert built.generic_target.recent_daily_returns == [-0.09999999999999998]
    assert built.policy_target.completed_account_wealth == pytest.approx(0.9)

    transaction = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    ).begin(
        operation="settlement_roll_forward",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260826",
        account_identity_digest="b" * 64,
        account_snapshot=_settlement_account(),
        account_day_continuity_digest="e" * 64,
        operation_nonce=_OPERATION_NONCE,
        operator_reason="record the first complete live-owned account day",
    )

    assert transaction.policy_target.completed_account_wealth == pytest.approx(0.9)
    assert transaction.policy_target.last_completed_account_day == "20260825"
    assert transaction.generic_target.recent_daily_returns == [-0.09999999999999998]


def test_settlement_roll_forward_rejects_current_day_cash_flow_for_explicit_rebase(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    policy = policy_store.save(
        replace(
            policy.state,
            live_inception_day="20260824",
            live_inception_equity=700_000.0,
        ),
        expected_sequence=policy.sequence,
    )
    generic_target, policy_target = _settlement_roll_forward_targets(
        generic,
        policy,
        include_completed_day=True,
    )
    generic_target = replace(
        generic_target,
        day_start_equity=630_100.0,
        last_account_deposit=100.0,
    )

    with pytest.raises(Stress90LifecycleTransactionError, match="cash flow.*rebase"):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="settlement_roll_forward",
            generic_source=generic,
            policy_source=policy,
            generic_target=generic_target,
            policy_target=policy_target,
            trading_day="20260826",
            account_identity_digest="b" * 64,
            account_snapshot=_settlement_account(deposit=100.0),
            account_day_continuity_digest="e" * 64,
            operation_nonce=_OPERATION_NONCE,
            operator_reason="cash flow needs explicit account rebase",
        )


def test_settlement_roll_forward_hard_hwm_includes_completed_prebalance() -> None:
    from afuture.stress90_lifecycle_transaction import stress90_lifecycle_account_transition

    source = RuntimeState(
        kill_switch=True,
        runtime_mode=RuntimeMode.HALTED.value,
        reconciled=True,
        trading_day="20260825",
        day_start_equity=100.0,
        equity_high_watermark=110.0,
        last_account_equity=109.0,
        last_account_trading_day="20260825",
        last_account_cash_flow_verified=True,
        last_account_settlement_id=43,
    )
    current = AccountSnapshot(
        100.0,
        100.0,
        100.0,
        0.0,
        0.0,
        0.0,
        "20260826",
        previous_settlement_equity=120.0,
        settlement_verified=True,
        settlement_id=44,
    )

    transition = stress90_lifecycle_account_transition(
        operation="settlement_roll_forward",
        generic_source=source,
        account_snapshot=current,
        account_switched=False,
    )

    assert transition.day_start_equity == 120.0
    assert transition.equity_high_watermark == 120.0


@pytest.mark.parametrize(
    ("deposit", "withdrawal", "expected_day_start", "expected_hwm"),
    [
        (100.0, 0.0, 850.0, 850.0),
        (0.0, 100.0, 650.0, 650.0),
    ],
)
def test_cross_day_account_rebase_settles_prior_day_before_current_cash_flow(
    deposit: float,
    withdrawal: float,
    expected_day_start: float,
    expected_hwm: float,
) -> None:
    from afuture.stress90_lifecycle_transaction import stress90_lifecycle_account_transition

    source = RuntimeState(
        kill_switch=True,
        runtime_mode=RuntimeMode.HALTED.value,
        reconciled=True,
        trading_day="20260825",
        day_start_equity=700.0,
        equity_high_watermark=700.0,
        last_account_equity=690.0,
        last_account_trading_day="20260825",
        last_account_cash_flow_verified=True,
        last_account_settlement_id=43,
    )
    current = AccountSnapshot(
        expected_day_start,
        expected_day_start,
        expected_day_start,
        0.0,
        0.0,
        0.0,
        "20260826",
        deposit=deposit,
        withdrawal=withdrawal,
        previous_settlement_equity=750.0,
        settlement_verified=True,
        settlement_id=44,
    )

    transition = stress90_lifecycle_account_transition(
        operation="account_rebase",
        generic_source=source,
        account_snapshot=current,
        account_switched=False,
    )

    assert transition.day_start_equity == expected_day_start
    assert transition.equity_high_watermark == expected_hwm
    assert transition.verified_deposit_delta == deposit
    assert transition.verified_withdrawal_delta == withdrawal


def test_cross_day_cash_flow_rebase_commits_both_states_with_continuity_evidence(
    tmp_path: Path,
) -> None:
    from afuture.directional_stress90_state import (
        REBASE_CONFIRMATION,
        rebase_stress90_account,
    )
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionStore,
        apply_stress90_lifecycle_transaction,
        derive_stress90_account_epoch,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    policy = policy_store.save(
        replace(
            policy.state,
            live_inception_day="20260824",
            live_inception_equity=700_000.0,
        ),
        expected_sequence=policy.sequence,
    )
    account = AccountSnapshot(
        850_000.0,
        850_000.0,
        850_000.0,
        0.0,
        0.0,
        0.0,
        "20260826",
        deposit=100_000.0,
        previous_settlement_equity=750_000.0,
        settlement_verified=True,
        settlement_id=44,
    )
    next_epoch = derive_stress90_account_epoch(
        operation="account_rebase",
        operation_nonce=_OPERATION_NONCE,
        policy_source_checksum=policy.checksum,
        account_identity_digest="b" * 64,
        trading_day="20260826",
    )
    policy_target, _audit = rebase_stress90_account(
        policy.state,
        account_trading_day="20260826",
        account_equity=850_000.0,
        operator_reason="verified deposit after prior-day settlement",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        strong_confirmation=REBASE_CONFIRMATION,
        account_identity_digest="b" * 64,
        account_epoch=next_epoch,
    )
    generic_target = replace(
        generic.state,
        kill_reason="Stress-90 account path rebased; doctor/Shadow gates remain required",
        trading_day="20260826",
        day_start_equity=850_000.0,
        equity_high_watermark=850_000.0,
        metadata_verified=False,
        last_account_equity=850_000.0,
        last_account_trading_day="20260826",
        last_account_deposit=100_000.0,
        last_account_withdrawal=0.0,
        last_account_cash_flow_verified=True,
        last_account_settlement_id=44,
        directional_daily_circuit_day="",
        recent_daily_returns=[],
    )
    transaction_store = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    )
    transaction = transaction_store.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260826",
        account_identity_digest="b" * 64,
        account_snapshot=account,
        account_day_continuity_digest="e" * 64,
        operation_nonce=_OPERATION_NONCE,
        operator_reason="verified deposit after prior-day settlement",
    )

    assert transaction.verified_deposit_delta == 100_000.0
    assert transaction.account_day_continuity_digest == "e" * 64
    assert transaction.account_day_continuity_source_day == "20260825"
    completed = apply_stress90_lifecycle_transaction(
        transaction_store,
        generic_store=generic_store,
        policy_store=policy_store,
    )
    assert completed.status == "committed"
    assert generic_store.load() == generic_target
    assert policy_store.load_required() == policy_target


def test_lifecycle_transaction_resumes_after_both_states_before_commit_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionStore,
        apply_stress90_lifecycle_transaction,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    transaction_store = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    )
    transaction_store.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )
    original_commit = transaction_store.mark_committed

    def crash_before_commit(*args, **kwargs):
        raise RuntimeError("crash before transaction commit")

    monkeypatch.setattr(transaction_store, "mark_committed", crash_before_commit)
    with pytest.raises(RuntimeError, match="transaction commit"):
        apply_stress90_lifecycle_transaction(
            transaction_store,
            generic_store=generic_store,
            policy_store=policy_store,
        )
    assert generic_store.load() == generic_target
    assert policy_store.load_required() == policy_target

    monkeypatch.setattr(transaction_store, "mark_committed", original_commit)
    assert (
        apply_stress90_lifecycle_transaction(
            transaction_store,
            generic_store=generic_store,
            policy_store=policy_store,
        ).status
        == "committed"
    )


def test_lifecycle_precommit_failure_keeps_transaction_prepared(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionStore,
        apply_stress90_lifecycle_transaction,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    transaction_store = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    )
    transaction_store.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )
    calls = 0

    def reject_stale_mechanical_evidence() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("mechanical evidence changed before lifecycle commit")

    with pytest.raises(RuntimeError, match="mechanical evidence changed"):
        apply_stress90_lifecycle_transaction(
            transaction_store,
            generic_store=generic_store,
            policy_store=policy_store,
            precommit_check=reject_stale_mechanical_evidence,
        )

    assert calls == 1
    assert generic_store.load() == generic_target
    assert policy_store.load_required() == policy_target
    assert transaction_store.load_required().status == "prepared"

    completed = apply_stress90_lifecycle_transaction(
        transaction_store,
        generic_store=generic_store,
        policy_store=policy_store,
        precommit_check=lambda: None,
    )
    assert completed.status == "committed"


def test_pending_lifecycle_transaction_blocks_runtime_and_unrelated_begin(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        require_no_pending_stress90_lifecycle_transaction,
    )

    generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    transaction_store = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    )
    transaction_store.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )

    with pytest.raises(Stress90LifecycleTransactionError, match="pending"):
        require_no_pending_stress90_lifecycle_transaction(tmp_path)
    with pytest.raises(Stress90LifecycleTransactionError, match="pending"):
        transaction_store.begin(
            operation="account_rebase",
            generic_source=generic_store.load_required_record(),
            policy_source=policy,
            generic_target=generic_target,
            policy_target=policy_target,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=_account(),
            operation_nonce="e" * 64,
        )


def test_lifecycle_transaction_refuses_unrelated_state_instead_of_rollback(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        apply_stress90_lifecycle_transaction,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    transaction_store = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    )
    transaction_store.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )
    generic_store.save(
        replace(generic.state, kill_reason="unrelated concurrent mutation"),
        expected_sequence=generic.sequence,
    )

    with pytest.raises(Stress90LifecycleTransactionError, match="unrelated"):
        apply_stress90_lifecycle_transaction(
            transaction_store,
            generic_store=generic_store,
            policy_store=policy_store,
        )


def test_account_rebase_begin_requires_a_matching_stress90_source_identity(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    _generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    unmarked_source = replace(
        generic,
        state=replace(generic.state, strategy_states={}),
    )

    with pytest.raises(Stress90LifecycleTransactionError, match="source policy identity"):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="account_rebase",
            generic_source=unmarked_source,
            policy_source=policy,
            generic_target=generic_target,
            policy_target=policy_target,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=_account(),
            operation_nonce=_OPERATION_NONCE,
        )


def test_lifecycle_transaction_binds_the_full_account_snapshot_digest(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        require_matching_stress90_lifecycle_account_evidence,
    )

    _generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    account = replace(
        _account(),
        available=690_000.0,
        margin=10_000.0,
        realized_pnl=123.0,
        unrealized_pnl=-45.0,
    )
    transaction = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    ).begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=account,
        operation_nonce=_OPERATION_NONCE,
    )

    assert transaction.account_evidence_scope == "account_snapshot"
    require_matching_stress90_lifecycle_account_evidence(
        transaction,
        account,
        account_identity_digest="b" * 64,
    )
    with pytest.raises(Stress90LifecycleTransactionError, match="evidence changed"):
        require_matching_stress90_lifecycle_account_evidence(
            transaction,
            replace(account, available=689_999.0),
            account_identity_digest="b" * 64,
        )


def test_settlement_retry_binds_immutable_settlement_evidence_not_intraday_marks(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        require_matching_stress90_lifecycle_account_evidence,
    )

    _generic_store, policy_store, generic, policy = _stores(tmp_path)
    policy = policy_store.save(
        replace(
            policy.state,
            live_inception_day="20260824",
            live_inception_equity=700_000.0,
        ),
        expected_sequence=policy.sequence,
    )
    generic_target, policy_target = _settlement_roll_forward_targets(
        generic,
        policy,
        include_completed_day=True,
    )
    account = _settlement_account()
    transaction = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    ).begin(
        operation="settlement_roll_forward",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260826",
        account_identity_digest="b" * 64,
        account_snapshot=account,
        account_day_continuity_digest="e" * 64,
        operation_nonce=_OPERATION_NONCE,
        operator_reason="resume an exact settlement after a coordinator crash",
    )

    assert transaction.account_evidence_scope == "settlement_snapshot"
    require_matching_stress90_lifecycle_account_evidence(
        transaction,
        replace(
            account,
            balance=624_500.0,
            equity=624_000.0,
            available=623_000.0,
            margin=1_000.0,
            realized_pnl=-250.0,
            unrealized_pnl=-500.0,
        ),
        account_identity_digest="b" * 64,
    )
    with pytest.raises(Stress90LifecycleTransactionError, match="evidence changed"):
        require_matching_stress90_lifecycle_account_evidence(
            transaction,
            replace(account, settlement_id=45),
            account_identity_digest="b" * 64,
        )


def test_lifecycle_begin_rejects_missing_account_snapshot(tmp_path: Path) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    _generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    with pytest.raises(Stress90LifecycleTransactionError, match="exact verified account"):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="account_rebase",
            generic_source=generic,
            policy_source=policy,
            generic_target=generic_target,
            policy_target=policy_target,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=None,  # type: ignore[arg-type]
            operation_nonce=_OPERATION_NONCE,
        )


def test_fresh_activation_initializes_authoritative_live_inception_day(
    tmp_path: Path,
) -> None:
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        Stress90CandidateState,
    )
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        bind_stress90_account_identity,
    )
    from afuture.models import AccountSnapshot
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        _validate_begin_source_invariants,
        derive_stress90_account_epoch,
    )

    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260824",
        last_decision_digest="a" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "1" * 64},
        bootstrap_through_day="20260824",
        last_completed_input_day="20260821",
    )
    policy_source = Stress90PolicyStateStore(tmp_path / "stress90_policy_state.json").save(
        Stress90PolicyState.from_seed(seed)
    )
    generic_target = activate_stress90_policy(
        RuntimeState(
            kill_switch=True,
            runtime_mode=RuntimeMode.HALTED.value,
            reconciled=True,
            trading_day="20260825",
            day_start_equity=700_000.0,
            equity_high_watermark=700_000.0,
            last_account_equity=700_000.0,
            last_account_trading_day="20260825",
            last_account_cash_flow_verified=True,
            last_account_settlement_id=43,
        ),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=seed.seed_digest,
        account_identity_digest="b" * 64,
        risk_overlay_digest="e" * 64,
        operator_reason="fresh authoritative activation",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    bound = bind_stress90_account_identity(
        policy_source.state,
        "b" * 64,
        account_epoch=derive_stress90_account_epoch(
            operation="activation",
            operation_nonce=_OPERATION_NONCE,
            policy_source_checksum=policy_source.checksum,
            account_identity_digest="b" * 64,
            trading_day="20260825",
        ),
    )
    account = AccountSnapshot(
        700_000.0,
        700_000.0,
        700_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
        previous_settlement_equity=700_000.0,
        settlement_verified=True,
        settlement_id=43,
    )
    lifecycle = Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")

    markerless_source = StateStore(tmp_path / "existing-state.json").save(
        replace(generic_target, strategy_states={})
    )
    with pytest.raises(
        Stress90LifecycleTransactionError,
        match="explicit execution-aligned identity",
    ):
        _validate_begin_source_invariants(
            operation="activation",
            generic_source=markerless_source,
            policy_source=policy_source,
            policy_target=replace(
                bound,
                live_inception_day="20260825",
                live_inception_equity=700_000.0,
            ),
            account_identity_digest="b" * 64,
            trading_day="20260825",
        )

    with pytest.raises(Stress90LifecycleTransactionError, match="initialization day"):
        lifecycle.begin(
            operation="activation",
            generic_source=None,
            policy_source=policy_source,
            generic_target=generic_target,
            policy_target=bound,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=account,
            operation_nonce=_OPERATION_NONCE,
            operator_reason="fresh authoritative activation",
        )

    with pytest.raises(Stress90LifecycleTransactionError, match="inception equity"):
        lifecycle.begin(
            operation="activation",
            generic_source=None,
            policy_source=policy_source,
            generic_target=generic_target,
            policy_target=replace(
                bound,
                live_inception_day="20260825",
                live_inception_equity=699_999.0,
            ),
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=account,
            operation_nonce=_OPERATION_NONCE,
            operator_reason="fresh authoritative activation",
        )

    transaction = lifecycle.begin(
        operation="activation",
        generic_source=None,
        policy_source=policy_source,
        generic_target=generic_target,
        policy_target=replace(
            bound,
            live_inception_day="20260825",
            live_inception_equity=700_000.0,
        ),
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=account,
        operation_nonce=_OPERATION_NONCE,
        operator_reason="fresh authoritative activation",
    )
    assert transaction.policy_target.live_inception_day == "20260825"


def test_account_epoch_derivation_is_lineage_bound_and_nonce_reuse_safe() -> None:
    from afuture.stress90_lifecycle_transaction import derive_stress90_account_epoch

    first = derive_stress90_account_epoch(
        operation="account_rebase",
        operation_nonce="a" * 64,
        policy_source_checksum="b" * 64,
        account_identity_digest="c" * 64,
        trading_day="20260825",
    )
    second = derive_stress90_account_epoch(
        operation="account_rebase",
        operation_nonce="a" * 64,
        policy_source_checksum="d" * 64,
        account_identity_digest="c" * 64,
        trading_day="20260825",
    )

    assert first != second
    assert first != "a" * 64
    assert second != "a" * 64


def test_same_day_migrated_execution_aligned_state_can_only_reactivate_with_new_epoch(
    tmp_path: Path,
) -> None:
    from afuture.directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        migrate_stress90_to_execution_aligned,
    )
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionStore,
        derive_stress90_account_epoch,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    generic = generic_store.save(
        replace(generic.state, recent_daily_returns=[-0.10]),
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )
    policy = policy_store.save(
        replace(
            policy.state,
            completed_account_wealth=0.90,
            completed_account_high_watermark=1.0,
            last_completed_account_day="20260824",
            recent_daily_returns_for_adaptive_margin=(-0.10,),
            live_inception_day="20260824",
            live_inception_equity=700_000.0,
        ),
        expected_sequence=policy.sequence,
    )
    migrated = migrate_stress90_to_execution_aligned(
        generic.state,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        account_identity_digest="b" * 64,
        operator_reason="temporarily run execution-aligned",
        strong_confirmation=DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
    )
    generic = generic_store.save(
        migrated,
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )
    target_generic = replace(
        activate_stress90_policy(
            migrated,
            broker_flat=True,
            local_flat=True,
            no_active_orders=True,
            reconciled=True,
            bootstrap_seed_digest=policy.state.bootstrap_seed_digest,
            account_identity_digest="b" * 64,
            risk_overlay_digest="e" * 64,
            operator_reason="explicit same-day Stress-90 reactivation",
            strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
        ),
        recent_daily_returns=[],
    )
    target_policy = replace(
        policy.state,
        completed_account_wealth=1.0,
        completed_account_high_watermark=1.0,
        last_completed_account_day=None,
        recent_daily_returns_for_adaptive_margin=(),
        live_inception_day="20260825",
        live_inception_equity=700_000.0,
        live_account_epoch=derive_stress90_account_epoch(
            operation="reactivation",
            operation_nonce=_OPERATION_NONCE,
            policy_source_checksum=policy.checksum,
            account_identity_digest="b" * 64,
            trading_day="20260825",
        ),
    )
    account = replace(_account(), withdrawal=0.0)

    transaction = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    ).begin(
        operation="reactivation",
        generic_source=generic,
        policy_source=policy,
        generic_target=target_generic,
        policy_target=target_policy,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=account,
        operation_nonce=_OPERATION_NONCE,
        operator_reason="explicit same-day Stress-90 reactivation",
    )

    assert transaction.operation == "reactivation"
    assert transaction.source_account_epoch == _SOURCE_ACCOUNT_EPOCH
    assert transaction.generic_target.day_start_equity == generic.state.day_start_equity
    assert transaction.generic_target.equity_high_watermark == generic.state.equity_high_watermark
    assert transaction.generic_target.recent_daily_returns == []
    assert transaction.policy_target.recent_daily_returns_for_adaptive_margin == ()
    assert transaction.policy_target.completed_account_wealth == 1.0
    assert transaction.policy_target.last_completed_account_day is None
    assert transaction.policy_target.live_account_epoch != _SOURCE_ACCOUNT_EPOCH


def test_reactivation_rejects_divergent_generic_and_policy_return_windows(
    tmp_path: Path,
) -> None:
    from afuture.directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        migrate_stress90_to_execution_aligned,
    )
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        derive_stress90_account_epoch,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    returns = [-0.08, 0.03]
    generic = generic_store.save(
        replace(generic.state, recent_daily_returns=returns),
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )
    policy = policy_store.save(
        replace(
            policy.state,
            recent_daily_returns_for_adaptive_margin=tuple(returns),
            live_inception_day="20260824",
            live_inception_equity=700_000.0,
        ),
        expected_sequence=policy.sequence,
    )
    migrated = migrate_stress90_to_execution_aligned(
        generic.state,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        account_identity_digest="b" * 64,
        operator_reason="temporary migration",
        strong_confirmation=DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
    )
    generic = generic_store.save(
        migrated,
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )
    target_generic = activate_stress90_policy(
        migrated,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=policy.state.bootstrap_seed_digest,
        account_identity_digest="b" * 64,
        risk_overlay_digest="e" * 64,
        operator_reason="reactivate",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    target_policy = replace(
        policy.state,
        completed_account_wealth=1.0,
        completed_account_high_watermark=1.0,
        last_completed_account_day=None,
        recent_daily_returns_for_adaptive_margin=(),
        live_inception_day="20260825",
        live_inception_equity=700_000.0,
        live_account_epoch=derive_stress90_account_epoch(
            operation="reactivation",
            operation_nonce=_OPERATION_NONCE,
            policy_source_checksum=policy.checksum,
            account_identity_digest="b" * 64,
            trading_day="20260825",
        ),
    )

    with pytest.raises(Stress90LifecycleTransactionError, match="return window"):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="reactivation",
            generic_source=generic,
            policy_source=policy,
            generic_target=target_generic,
            policy_target=target_policy,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=replace(_account(), withdrawal=0.0),
            operation_nonce=_OPERATION_NONCE,
            operator_reason="reactivate",
        )


def test_lifecycle_lineage_marker_prevents_deleted_coordinator_recreation(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    _generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    store = Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
    begin_kwargs = {
        "operation": "account_rebase",
        "generic_source": generic,
        "policy_source": policy,
        "generic_target": generic_target,
        "policy_target": policy_target,
        "trading_day": "20260825",
        "account_identity_digest": "b" * 64,
        "account_snapshot": _account(),
        "operation_nonce": _OPERATION_NONCE,
    }
    store.begin(**begin_kwargs)

    assert store.lineage_path.is_file()
    store.path.unlink()
    store.previous_path.unlink(missing_ok=True)

    with pytest.raises(Stress90LifecycleTransactionError, match="lineage.*missing"):
        store.load()
    with pytest.raises(Stress90LifecycleTransactionError, match="lineage.*missing"):
        store.begin(**begin_kwargs)


@pytest.mark.parametrize("failure_target", ["marker", "parent"])
def test_lifecycle_marker_fsync_failure_never_accepts_a_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    _generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    store = Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
    real_fsync = os.fsync

    def fail_selected_fsync(descriptor: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if (failure_target == "parent") is is_directory:
            raise OSError(f"injected {failure_target} fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_selected_fsync)
    with pytest.raises(Stress90LifecycleTransactionError, match="lineage marker"):
        store.begin(
            operation="account_rebase",
            generic_source=generic,
            policy_source=policy,
            generic_target=generic_target,
            policy_target=policy_target,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=_account(),
            operation_nonce=_OPERATION_NONCE,
        )

    assert store.lineage_path.exists()
    assert not store.path.exists()
    monkeypatch.setattr(os, "fsync", real_fsync)
    with pytest.raises(Stress90LifecycleTransactionError, match="lineage.*missing"):
        store.load()


def test_markerless_lifecycle_layout_fails_closed_without_rewriting(
    tmp_path: Path,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    _generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    store = Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
    store.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )
    store.lineage_path.unlink()
    original = store.path.read_bytes()

    with pytest.raises(Stress90LifecycleTransactionError, match="lineage marker is missing"):
        Stress90LifecycleTransactionStore(store.path).load_required()

    assert store.path.read_bytes() == original
    assert not store.lineage_path.exists()


def test_lifecycle_requires_adjacent_previous_evidence_after_initial_record(
    tmp_path: Path,
) -> None:
    from afuture import stress90_lifecycle_transaction as lifecycle_module
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    _generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    store = Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
    transaction = store.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )
    initial_bytes = store.path.read_bytes()
    store.mark_committed(transaction.transaction_id)
    current_bytes = store.path.read_bytes()

    store.previous_path.unlink()
    with pytest.raises(
        Stress90LifecycleTransactionError,
        match="previous lifecycle transaction evidence is missing",
    ):
        store.load_required()
    assert store.path.read_bytes() == current_bytes

    nonadjacent = json.loads(initial_bytes)
    nonadjacent["sequence"] = 3
    unsigned = {key: value for key, value in nonadjacent.items() if key != "checksum"}
    nonadjacent["checksum"] = lifecycle_module._digest(unsigned)
    nonadjacent_bytes = json.dumps(nonadjacent, sort_keys=True).encode()
    store.path.write_bytes(nonadjacent_bytes)
    store.previous_path.write_bytes(initial_bytes)
    with pytest.raises(
        Stress90LifecycleTransactionError,
        match="previous lifecycle transaction sequence is not adjacent",
    ):
        store.load_required()
    assert store.path.read_bytes() == nonadjacent_bytes
    assert store.previous_path.read_bytes() == initial_bytes


def test_lifecycle_rejects_non_sha_target_marker_risk_overlay(tmp_path: Path) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        _validate_operation_invariants,
    )

    _generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    transaction = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    ).begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )
    marker = dict(transaction.generic_target.strategy_states["directional_policy_identity"])
    marker["risk_overlay_digest"] = "not-a-sha256-digest"
    invalid_target = replace(
        transaction.generic_target,
        strategy_states={"directional_policy_identity": marker},
    )

    with pytest.raises(
        Stress90LifecycleTransactionError,
        match="cross-file policy identity mismatch",
    ):
        _validate_operation_invariants(replace(transaction, generic_target=invalid_target))


@pytest.mark.parametrize("failure_target", ["marker", "parent"])
def test_existing_lifecycle_marker_refsync_failure_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    _generic_store, _policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    store = Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
    store.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )
    real_fsync = os.fsync

    def fail_selected_fsync(descriptor: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if (failure_target == "parent") is is_directory:
            raise OSError(f"injected existing marker {failure_target} fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_selected_fsync)
    with pytest.raises(Stress90LifecycleTransactionError, match="lineage marker.*durability"):
        store.load_required()


@pytest.mark.parametrize(
    ("current_sequence", "current_status", "previous_sequence"),
    [
        (2, "prepared", None),
        (3, "committed", 1),
        (4, "committed", 2),
    ],
)
def test_lifecycle_rejects_impossible_sequence_status_parity(
    tmp_path: Path,
    current_sequence: int,
    current_status: str,
    previous_sequence: int | None,
) -> None:
    from afuture import stress90_lifecycle_transaction as lifecycle_module
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        apply_stress90_lifecycle_transaction,
    )

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    generic_target, policy_target = _targets(generic, policy)
    store = Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
    store.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=_account(),
        operation_nonce=_OPERATION_NONCE,
    )
    if current_status == "committed":
        apply_stress90_lifecycle_transaction(
            store,
            generic_store=generic_store,
            policy_store=policy_store,
        )

    def rewrite_sequence(path: Path, sequence: int) -> None:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["sequence"] = sequence
        unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
        envelope["checksum"] = lifecycle_module._digest(unsigned)
        path.write_text(json.dumps(envelope), encoding="utf-8")

    rewrite_sequence(store.path, current_sequence)
    if previous_sequence is None:
        store.previous_path.unlink(missing_ok=True)
    else:
        rewrite_sequence(store.previous_path, previous_sequence)

    with pytest.raises(Stress90LifecycleTransactionError, match="sequence/status parity"):
        Stress90LifecycleTransactionStore(store.path).load_required()


def test_real_prepared_migration_retries_after_registry_acknowledgement_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    from afuture import stress90_lifecycle_transaction as lifecycle_module
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )
    from afuture.cli import (
        _apply_stress90_account_runtime_registry_transition,
        _apply_stress90_trading_day_evidence_transition,
        _commit_stress90_lifecycle_under_broker_fence,
    )
    from afuture.directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        migrate_stress90_to_execution_aligned,
    )
    from afuture.directional_stress90_state import Stress90PolicyStateStore
    from afuture.stress90_lifecycle_transaction import Stress90LifecycleTransactionStore

    generic_store, policy_store, generic, policy = _stores(tmp_path)
    migrated = migrate_stress90_to_execution_aligned(
        generic.state,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        account_identity_digest="b" * 64,
        operator_reason="durable migration retry",
        strong_confirmation=DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
    )
    lifecycle_path = tmp_path / "stress90_lifecycle_transaction.json"
    lifecycle_store = Stress90LifecycleTransactionStore(lifecycle_path)
    prepared = lifecycle_store.begin(
        operation="stress90_to_execution_aligned",
        generic_source=generic,
        policy_source=policy,
        generic_target=migrated,
        policy_target=policy.state,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=replace(_account(), withdrawal=0.0),
        operation_nonce="f" * 64,
        operator_reason="durable migration retry",
    )
    registry = AccountRuntimeRegistry(tmp_path / ".account-runtime-registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    registry.bind_new("b" * 64, tmp_path, _SOURCE_ACCOUNT_EPOCH, "e" * 64)
    from afuture.trading_day_evidence import TradingDayEvidenceStore

    evidence_store = TradingDayEvidenceStore(tmp_path / "ctp_trading_day_evidence.json")
    evidence_store.bind_for_lifecycle(
        transaction=SimpleNamespace(
            operation="activation",
            status="committed",
            transaction_id="d" * 64,
            operation_nonce="e" * 64,
            source_account_identity_digest="",
            source_account_epoch="",
            account_identity_digest="b" * 64,
            trading_day="20260825",
            policy_target=SimpleNamespace(
                live_account_identity_digest="b" * 64,
                live_account_epoch=_SOURCE_ACCOUNT_EPOCH,
            ),
        ),
        runtime_dir=tmp_path,
        binding_evidence=registry.require_binding_evidence(
            "b" * 64,
            tmp_path,
            _SOURCE_ACCOUNT_EPOCH,
        ),
    )
    registry_before = registry.load_required()
    config = SimpleNamespace(
        state_path=str(tmp_path / "state.json"),
        account_registry_path=str(tmp_path / ".account-runtime-registry.json"),
    )

    class Broker:
        @contextmanager
        def lifecycle_state_commit_fence(self):
            yield

    real_apply = lifecycle_module.apply_stress90_lifecycle_transaction

    def crash_before_evidence(_transaction) -> None:
        raise OSError("injected post-registry evidence crash")

    with pytest.raises(OSError, match="post-registry evidence crash"):
        _commit_stress90_lifecycle_under_broker_fence(
            Broker(),
            transaction_store=lifecycle_store,
            generic_store=generic_store,
            policy_store=policy_store,
            prepare_transaction=lambda: prepared,
            apply_registry_transition=lambda transaction: (
                _apply_stress90_account_runtime_registry_transition(
                    config,
                    runtime_dir=tmp_path,
                    lifecycle_transaction=transaction,
                )
            ),
            apply_evidence_transition=crash_before_evidence,
            precommit_check=lambda: None,
        )

    acknowledged = AccountRuntimeRegistry(registry.path).load_required()
    evidence_before_retry = evidence_store.load_required()
    assert acknowledged.sequence == registry_before.sequence + 1
    assert evidence_before_retry.account_binding_last_operation_id == "e" * 64
    assert evidence_before_retry.rebind_transaction_id == "d" * 64
    assert lifecycle_store.load_required().status == "prepared"

    def crash_before_state_commit(*_args, **_kwargs):
        raise OSError("injected post-registry state crash")

    monkeypatch.setattr(
        lifecycle_module,
        "apply_stress90_lifecycle_transaction",
        crash_before_state_commit,
    )
    with pytest.raises(OSError, match="post-registry state crash"):
        _commit_stress90_lifecycle_under_broker_fence(
            Broker(),
            transaction_store=lifecycle_store,
            generic_store=generic_store,
            policy_store=policy_store,
            prepare_transaction=lambda: prepared,
            apply_registry_transition=lambda transaction: (
                _apply_stress90_account_runtime_registry_transition(
                    config,
                    runtime_dir=tmp_path,
                    lifecycle_transaction=transaction,
                )
            ),
            apply_evidence_transition=lambda transaction: (
                _apply_stress90_trading_day_evidence_transition(
                    config,
                    runtime_dir=tmp_path,
                    lifecycle_transaction=transaction,
                )
            ),
            precommit_check=lambda: None,
        )

    evidence_after_crash = evidence_store.load_required()
    assert AccountRuntimeRegistry(registry.path).load_required() == acknowledged
    assert evidence_after_crash.account_epoch == _SOURCE_ACCOUNT_EPOCH
    assert evidence_after_crash.account_binding_last_operation_id == prepared.operation_nonce
    assert evidence_after_crash.rebind_transaction_id == prepared.transaction_id
    assert lifecycle_store.load_required().status == "prepared"

    monkeypatch.setattr(lifecycle_module, "apply_stress90_lifecycle_transaction", real_apply)
    fresh_lifecycle = Stress90LifecycleTransactionStore(lifecycle_path)
    fresh_generic = StateStore(generic_store.path)
    fresh_policy = Stress90PolicyStateStore(policy_store.path)
    committed = _commit_stress90_lifecycle_under_broker_fence(
        Broker(),
        transaction_store=fresh_lifecycle,
        generic_store=fresh_generic,
        policy_store=fresh_policy,
        prepare_transaction=fresh_lifecycle.load_required,
        apply_registry_transition=lambda transaction: (
            _apply_stress90_account_runtime_registry_transition(
                config,
                runtime_dir=tmp_path,
                lifecycle_transaction=transaction,
            )
        ),
        apply_evidence_transition=lambda transaction: (
            _apply_stress90_trading_day_evidence_transition(
                config,
                runtime_dir=tmp_path,
                lifecycle_transaction=transaction,
            )
        ),
        precommit_check=lambda: None,
    )

    after_retry = AccountRuntimeRegistry(registry.path).load_required()
    assert committed.status == "committed"
    assert fresh_lifecycle.load_required().status == "committed"
    assert evidence_store.load_required() == evidence_after_crash
    assert after_retry.sequence == acknowledged.sequence
    assert after_retry.bindings[0].operation_history == (
        "e" * 64,
        prepared.operation_nonce,
    )


def test_rebase_cash_transition_uses_cumulative_daily_baseline_but_incremental_hwm() -> None:
    from afuture.stress90_lifecycle_transaction import (
        stress90_lifecycle_account_transition,
    )

    source = RuntimeState(
        trading_day="20260825",
        day_start_equity=1_100_000.0,
        equity_high_watermark=1_100_000.0,
        last_account_trading_day="20260825",
        last_account_deposit=100_000.0,
        last_account_withdrawal=0.0,
        last_account_cash_flow_verified=True,
        last_account_settlement_id=44,
    )
    account = AccountSnapshot(
        1_100_000.0,
        1_100_000.0,
        1_100_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
        deposit=150_000.0,
        previous_settlement_equity=1_000_000.0,
        settlement_verified=True,
        settlement_id=44,
    )

    transition = stress90_lifecycle_account_transition(
        operation="account_rebase",
        generic_source=source,
        account_snapshot=account,
        account_switched=False,
    )

    assert transition.verified_deposit_delta == 50_000.0
    assert transition.day_start_equity == 1_150_000.0
    assert transition.equity_high_watermark == 1_150_000.0


def test_same_account_cross_day_zero_cash_flow_requires_settlement_roll_forward() -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        stress90_lifecycle_account_transition,
    )

    source = RuntimeState(
        trading_day="20260825",
        day_start_equity=1_100_000.0,
        equity_high_watermark=1_100_000.0,
        last_account_trading_day="20260825",
        last_account_deposit=100_000.0,
        last_account_withdrawal=0.0,
        last_account_cash_flow_verified=True,
        last_account_settlement_id=44,
    )
    account = AccountSnapshot(
        1_200_000.0,
        1_200_000.0,
        1_200_000.0,
        0.0,
        0.0,
        0.0,
        "20260826",
        deposit=0.0,
        previous_settlement_equity=1_000_000.0,
        settlement_verified=True,
        settlement_id=45,
    )

    with pytest.raises(Stress90LifecycleTransactionError, match="settlement roll-forward"):
        stress90_lifecycle_account_transition(
            operation="account_rebase",
            generic_source=source,
            account_snapshot=account,
            account_switched=False,
        )


def test_same_day_execution_aligned_activation_preserves_hard_daily_baseline() -> None:
    from afuture.stress90_lifecycle_transaction import (
        stress90_lifecycle_account_transition,
    )

    source = RuntimeState(
        trading_day="20260825",
        day_start_equity=1_000_000.0,
        equity_high_watermark=1_000_000.0,
        last_account_equity=951_000.0,
        last_account_trading_day="20260825",
        last_account_cash_flow_verified=True,
        last_account_settlement_id=44,
    )
    account = AccountSnapshot(
        951_000.0,
        951_000.0,
        951_000.0,
        0.0,
        -49_000.0,
        0.0,
        "20260825",
        previous_settlement_equity=1_000_000.0,
        settlement_verified=True,
        settlement_id=44,
    )

    transition = stress90_lifecycle_account_transition(
        operation="activation",
        generic_source=source,
        account_snapshot=account,
        account_switched=False,
    )

    assert transition.day_start_equity == 1_000_000.0
    assert transition.equity_high_watermark == 1_000_000.0


@pytest.mark.parametrize(
    ("settlement_id", "previous_settlement_equity"),
    [(45, 1_000_000.0), (44, 1_100_000.0)],
    ids=("settlement-identity-changed", "prebalance-changed"),
)
def test_same_day_account_transition_rejects_settlement_replacement(
    settlement_id: int,
    previous_settlement_equity: float,
) -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        stress90_lifecycle_account_transition,
    )

    source = RuntimeState(
        trading_day="20260825",
        day_start_equity=1_000_000.0,
        equity_high_watermark=1_000_000.0,
        last_account_equity=951_000.0,
        last_account_trading_day="20260825",
        last_account_deposit=0.0,
        last_account_withdrawal=0.0,
        last_account_cash_flow_verified=True,
        last_account_settlement_id=44,
    )
    account = AccountSnapshot(
        1_100_000.0,
        1_100_000.0,
        1_100_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
        deposit=100_000.0,
        previous_settlement_equity=previous_settlement_equity,
        settlement_verified=True,
        settlement_id=settlement_id,
    )

    with pytest.raises(Stress90LifecycleTransactionError, match="same-day settlement"):
        stress90_lifecycle_account_transition(
            operation="account_rebase",
            generic_source=source,
            account_snapshot=account,
            account_switched=False,
        )


def test_existing_account_transition_rejects_partial_source_day_identity() -> None:
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        stress90_lifecycle_account_transition,
    )

    source = RuntimeState(
        day_start_equity=1_000_000.0,
        equity_high_watermark=1_000_000.0,
        last_account_trading_day="20260825",
        last_account_cash_flow_verified=True,
        last_account_settlement_id=44,
    )
    account = AccountSnapshot(
        951_000.0,
        951_000.0,
        951_000.0,
        0.0,
        -49_000.0,
        0.0,
        "20260825",
        previous_settlement_equity=1_000_000.0,
        settlement_verified=True,
        settlement_id=44,
    )

    with pytest.raises(Stress90LifecycleTransactionError, match="source day identity"):
        stress90_lifecycle_account_transition(
            operation="activation",
            generic_source=source,
            account_snapshot=account,
            account_switched=False,
        )
