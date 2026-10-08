from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.models import AccountSnapshot, RuntimeMode
from afuture.state import RuntimeState, StateStore

_ACCOUNT_EPOCH = "c" * 64


_OPERATION_NONCE = "d" * 64


@pytest.fixture(autouse=True)
def _use_isolated_current_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "afuture.account_runtime_registry.PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH",
        tmp_path / ".account-runtime-registry.json",
    )


def _seeded_stress90_runtime(runtime_dir: Path, *, account_identity: str = "b" * 64):
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        Stress90SeedStore,
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
    Stress90SeedStore(runtime_dir / "stress90_bootstrap_seed.json").save_new(seed)
    policy_store = Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json")
    policy = policy_store.save(
        bind_stress90_account_identity(
            Stress90PolicyState.from_seed(seed),
            account_identity,
            account_epoch=_ACCOUNT_EPOCH,
        )
    )
    generic_store = StateStore(runtime_dir / "state.json")
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
            account_identity_digest=account_identity,
            risk_overlay_digest="e" * 64,
            operator_reason="commissioned",
            strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
        )
    )
    registry = AccountRuntimeRegistry(runtime_dir / ".account-runtime-registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    registry.bind_new(
        account_identity,
        runtime_dir,
        _ACCOUNT_EPOCH,
        "e" * 64,
    )
    from afuture.trading_day_evidence import TradingDayEvidenceStore

    TradingDayEvidenceStore(runtime_dir / "ctp_trading_day_evidence.json").bind_for_lifecycle(
        transaction=SimpleNamespace(
            operation="activation",
            status="committed",
            transaction_id="f" * 64,
            operation_nonce="e" * 64,
            source_account_identity_digest="",
            source_account_epoch="",
            account_identity_digest=account_identity,
            trading_day="20260825",
            policy_target=SimpleNamespace(
                live_account_identity_digest=account_identity,
                live_account_epoch=_ACCOUNT_EPOCH,
            ),
        ),
        runtime_dir=runtime_dir,
        binding_evidence=registry.require_binding_evidence(
            account_identity,
            runtime_dir,
            _ACCOUNT_EPOCH,
        ),
    )
    return seed, generic_store, policy_store, generic, policy


def _prepare_account_rebase(runtime_dir: Path):
    from afuture.directional_stress90_state import rebase_stress90_account
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionStore,
        derive_stress90_account_epoch,
    )

    seed, generic_store, policy_store, generic, policy = _seeded_stress90_runtime(runtime_dir)
    policy_target, _audit = rebase_stress90_account(
        policy.state,
        account_trading_day="20260825",
        account_equity=700_000.0,
        operator_reason="cash transfer",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        strong_confirmation="RESET_STRESS90_ACCOUNT_PATH",
        account_identity_digest="b" * 64,
        account_epoch=derive_stress90_account_epoch(
            operation="account_rebase",
            operation_nonce=_OPERATION_NONCE,
            policy_source_checksum=policy.checksum,
            account_identity_digest="b" * 64,
            trading_day="20260825",
        ),
    )
    generic_target = replace(
        generic.state,
        trading_day="20260825",
        day_start_equity=699_000.0,
        equity_high_watermark=700_000.0,
        last_account_equity=700_000.0,
        last_account_trading_day="20260825",
        last_account_deposit=0.0,
        last_account_withdrawal=1_000.0,
        last_account_cash_flow_verified=True,
        last_account_settlement_id=43,
        reconciled=True,
        metadata_verified=False,
        kill_switch=True,
        kill_reason="Stress-90 account path rebased; doctor/Shadow gates remain required",
        runtime_mode=RuntimeMode.HALTED.value,
        directional_daily_circuit_day="",
    )
    lifecycle = Stress90LifecycleTransactionStore(
        runtime_dir / "stress90_lifecycle_transaction.json"
    )
    lifecycle.begin(
        operation="account_rebase",
        generic_source=generic,
        policy_source=policy,
        generic_target=generic_target,
        policy_target=policy_target,
        trading_day="20260825",
        account_identity_digest="b" * 64,
        account_snapshot=AccountSnapshot(
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
        ),
        operation_nonce=_OPERATION_NONCE,
        operator_reason="cash transfer",
    )
    return seed, generic_store, policy_store, lifecycle


def test_prepared_lifecycle_blocks_every_order_capable_runtime_not_only_directional(
    tmp_path: Path,
) -> None:
    from afuture.directional import DirectionalConfig
    from afuture.risk import RiskConfig
    from afuture.runtime_factory import build_runtime_engine
    from afuture.stress90_lifecycle_transaction import Stress90LifecycleTransactionError

    _prepare_account_rebase(tmp_path)
    config = SimpleNamespace(
        risk=RiskConfig(),
        auto_flatten_imbalance=True,
        aggressive_ticks=1,
        slippage_ticks=1,
        legging_timeout_seconds=2.0,
        require_live_metadata=False,
        metadata_timeout_seconds=10.0,
        directional=DirectionalConfig(enabled=False),
        pairs=[],
        contracts={},
    )

    with pytest.raises(Stress90LifecycleTransactionError, match="pending"):
        build_runtime_engine(config, object(), StateStore(tmp_path / "state.json"))


def test_lifecycle_begin_rejects_cross_file_account_identity_mismatch(tmp_path: Path) -> None:
    from afuture.directional_stress90_state import bind_stress90_account_identity
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    _seed, _generic_store, _policy_store, generic, policy = _seeded_stress90_runtime(tmp_path)
    mismatched_policy_target = bind_stress90_account_identity(
        policy.state,
        "c" * 64,
        account_epoch=_OPERATION_NONCE,
        allow_rebind=True,
    )

    with pytest.raises(Stress90LifecycleTransactionError, match="account identity"):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="account_rebase",
            generic_source=generic,
            policy_source=policy,
            generic_target=generic.state,
            policy_target=mismatched_policy_target,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=AccountSnapshot(
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
            ),
            operation_nonce=_OPERATION_NONCE,
        )
