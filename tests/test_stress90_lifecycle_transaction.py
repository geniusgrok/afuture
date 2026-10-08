from __future__ import annotations

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
