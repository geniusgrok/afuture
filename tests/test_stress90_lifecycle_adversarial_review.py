from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.models import AccountSnapshot, Offset, OrderSide, RuntimeMode, Trade
from afuture.state import RuntimeState, StateStore

_ACCOUNT_EPOCH = "c" * 64
_OPERATION_NONCE = "d" * 64


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


def _run_pending_rebase(
    runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    active_orders: list[object],
    equity: float = 700_000.0,
    operator_reason: str = "resume cash transfer",
    settlement_verified: bool = True,
    account_identity: str = "b" * 64,
    bypass_trading_day_checkpoint: bool = True,
    withdrawal: float = 1_000.0,
    operation_id: str = _OPERATION_NONCE,
    session_trades: list[Trade] | None = None,
    fence_observations: list[str] | None = None,
) -> int:
    from afuture.cli import _run_stress90_account_rebase
    from afuture.directional import DirectionalConfig
    from afuture.directional_stress90_state import REBASE_CONFIRMATION
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    observations = [] if fence_observations is None else fence_observations

    class FakeBroker:
        def __init__(self, credentials):
            self.credentials = credentials
            self.order_journal_configured = False
            self.fence_entries = 0

        def configure_order_submission_journal(self, path, **identity):
            assert Path(path) == runtime_dir / "stress90_ctp_orders.json"
            assert identity["policy_id"] == "directional.stress90"
            self.order_journal_configured = True

        def set_raw_tick_observer(self, _observer):
            return None

        def seed_trade_identities(self, identities):
            assert list(identities) == []

        def start(self):
            return None

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return marker == (0, 0)

        def get_account(self):
            return AccountSnapshot(
                equity,
                equity,
                equity,
                0,
                0,
                0,
                "20260825",
                withdrawal=withdrawal,
                previous_settlement_equity=(700_000.0 if settlement_verified else None),
                settlement_verified=settlement_verified,
                settlement_id=43,
            )

        def get_positions(self):
            return []

        def get_active_orders(self):
            return active_orders

        def get_session_trades(self):
            return list(session_trades or [])

        def refresh_session_activity(self, *, timeout_seconds: float):
            from afuture.broker.ctp_session_query import (
                build_ctp_session_activity_evidence,
            )

            assert timeout_seconds == 0.1
            return build_ctp_session_activity_evidence(
                account_identity_digest=account_identity,
                trading_day="20260825",
                order_request_id=11,
                trade_request_id=12,
                orders=(),
                trades=(),
                critical_generation=0,
            )

        def recover_stress90_session_activity(self, _evidence):
            if not self.order_journal_configured:
                raise RuntimeError("session recovery requires configured target journal")
            return ()

        def require_session_activity_evidence_current(self, _evidence):
            return None

        @contextmanager
        def lifecycle_state_commit_fence(self):
            self.fence_entries += 1
            observations.append("enter")
            try:
                yield
            finally:
                observations.append("exit")

        def get_contract_catalog(self):
            return []

        def owns_order(self, order_id):
            return order_id != "manual-order"

        def get_trading_day(self):
            return "20260825"

        def get_account_identity_digest(self):
            return account_identity

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    if bypass_trading_day_checkpoint:
        monkeypatch.setattr(
            "afuture.cli._checkpoint_ctp_trading_day",
            lambda *args, **kwargs: None,
        )
    monkeypatch.setenv("AFUTURE_STRESS90_REBASE_ACK", REBASE_CONFIRMATION)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(runtime_dir / "state.json"),
        journal_path=str(runtime_dir / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_rebase=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason=operator_reason,
        operation_id=operation_id,
        shadow_account=False,
    )
    return _run_stress90_account_rebase(config, args)


def test_account_rebase_rejects_unknown_completed_session_trade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seeded_stress90_runtime(tmp_path)
    unknown = Trade(
        trade_id="manual-trade",
        order_id="manual-order",
        symbol="A2609",
        exchange="DCE",
        side=OrderSide.BUY,
        offset=Offset.OPEN,
        volume=1,
        price=3_000.0,
        timestamp=datetime(2026, 8, 25, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(RuntimeError, match="unknown session trade"):
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason="manual trading must fail account lifecycle",
            session_trades=[unknown],
        )


def test_lifecycle_crash_fill_adoption_preserves_net_flat_fill_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _adopt_stress90_lifecycle_crash_fills
    from afuture.directional import DirectionalConfig
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    adopted = (
        "20260825:DCE:CTP.T-open",
        "20260825:DCE:CTP.T-close",
    )

    class AdoptionManager:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def reconcile_authorized_crash_fills(self, local, remote, recent):
            assert local == remote == []
            assert recent == ()
            return True, adopted, "exact durable net-flat fills"

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "afuture.directional_stress90_runtime.Stress90DirectionalPortfolioManager",
        AdoptionManager,
    )
    state = RuntimeState(
        kill_switch=True,
        reconciled=True,
        runtime_mode=RuntimeMode.HALTED.value,
    )
    result = _adopt_stress90_lifecycle_crash_fills(
        SimpleNamespace(
            directional=DirectionalConfig(
                enabled=True,
                policy="stress90",
                products=FROZEN_PRODUCTS,
                account_exclusive=True,
            )
        ),
        object(),
        runtime_dir=tmp_path,
        state=state,
        broker_positions=[],
    )

    assert result.recent_trade_ids == list(adopted)
    assert result.positions == []
    assert result.kill_switch is True


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


def test_activation_does_not_relabel_a_mismatched_stress90_definition() -> None:
    from afuture.directional_policy_activation import (
        POLICY_IDENTITY_STATE_KEY,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY

    corrupt_identity = RuntimeState(
        kill_switch=True,
        runtime_mode=RuntimeMode.HALTED.value,
        reconciled=True,
        strategy_states={
            POLICY_IDENTITY_STATE_KEY: {
                "policy_id": STRESS90_POLICY.policy_id,
                "policy_definition_digest": "f" * 64,
                "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
                "bootstrap_seed_digest": "a" * 64,
                "account_identity_digest": "b" * 64,
                "operator_reason": "old incompatible definition",
            }
        },
    )

    with pytest.raises(RuntimeError, match="(identity|digest|definition).*(mismatch|invalid)"):
        activate_stress90_policy(
            corrupt_identity,
            broker_flat=True,
            local_flat=True,
            no_active_orders=True,
            reconciled=True,
            bootstrap_seed_digest="a" * 64,
            account_identity_digest="b" * 64,
            operator_reason="must not rewrite corrupt identity",
            strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
        )


def test_activation_does_not_relabel_a_corrupt_execution_aligned_identity() -> None:
    from afuture.directional_policy_activation import (
        POLICY_IDENTITY_STATE_KEY,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY

    corrupt_identity = RuntimeState(
        kill_switch=True,
        runtime_mode=RuntimeMode.HALTED.value,
        reconciled=True,
        strategy_states={
            POLICY_IDENTITY_STATE_KEY: {
                "policy_id": "execution_aligned",
                "policy_definition_digest": "f" * 64,
                "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
                "account_identity_digest": "b" * 64,
                "operator_reason": "corrupt execution-aligned identity",
            }
        },
    )

    with pytest.raises(RuntimeError, match="(identity|digest|definition).*(mismatch|invalid)"):
        activate_stress90_policy(
            corrupt_identity,
            broker_flat=True,
            local_flat=True,
            no_active_orders=True,
            reconciled=True,
            bootstrap_seed_digest="a" * 64,
            account_identity_digest="b" * 64,
            operator_reason="must not rewrite corrupt identity",
            strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
        )


def test_pending_account_rebase_rechecks_no_active_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_account_rebase(tmp_path)

    with pytest.raises(RuntimeError, match="active order"):
        _run_pending_rebase(tmp_path, monkeypatch, active_orders=[object()])


def test_pending_account_rebase_is_bound_to_exact_account_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_account_rebase(tmp_path)

    with pytest.raises(RuntimeError, match="account (snapshot|evidence).*(changed|mismatch)"):
        _run_pending_rebase(tmp_path, monkeypatch, active_orders=[], equity=710_000.0)


def test_pending_account_rebase_rejects_a_different_operator_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_account_rebase(tmp_path)

    with pytest.raises(RuntimeError, match="operator reason.*(changed|mismatch)"):
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason="different cash-transfer explanation",
        )


def test_retry_after_committed_rebase_does_not_create_a_second_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.stress90_lifecycle_transaction import apply_stress90_lifecycle_transaction

    _seed, generic_store, policy_store, lifecycle = _prepare_account_rebase(tmp_path)
    first = apply_stress90_lifecycle_transaction(
        lifecycle,
        generic_store=generic_store,
        policy_store=policy_store,
    )
    generic_sequence = generic_store.load_required_record().sequence
    policy_sequence = policy_store.load_required_record().sequence
    fence_observations: list[str] = []

    assert (
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason="cash transfer",
            fence_observations=fence_observations,
        )
        == 0
    )
    assert (
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason="cash transfer",
            fence_observations=fence_observations,
        )
        == 0
    )

    assert lifecycle.load_required().transaction_id == first.transaction_id
    assert generic_store.load_required_record().sequence == generic_sequence
    assert policy_store.load_required_record().sequence == policy_sequence
    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8").count(
        '"event_type":"stress90_account_rebase_completed"'
    ) == 1
    assert fence_observations == ["enter", "exit", "enter", "exit"]


def test_account_switch_rebase_resumes_after_order_epoch_cleanup_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture import cli
    from afuture.broker.ctp_order_journal import (
        CtpOrderJournalIntegrityError,
        CtpOrderSubmissionJournal,
    )
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )
    from afuture.stress90_lifecycle_transaction import Stress90LifecycleTransactionStore

    _seed, generic_store, policy_store, _generic, _policy = _seeded_stress90_runtime(
        tmp_path,
        account_identity="b" * 64,
    )
    intent_store = Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json")
    prepare_stress90_execution_intent(
        intent_store,
        target_trading_day="20260825",
        daily_decision_digest="9" * 64,
        account_identity_digest="b" * 64,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    reason = "switch to a separately reconciled account after durable epoch seal"
    timeline: list[str] = []
    cleanup_attempts = 0
    original_cleanup = CtpOrderSubmissionJournal._cleanup_sealed_epoch_sources
    original_full_audit = cli._require_stress90_order_journal_full_audit

    def crash_first_cleanup(self, epoch):
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        timeline.append("cleanup")
        if cleanup_attempts == 1:
            raise OSError("injected account-switch epoch cleanup crash")
        return original_cleanup(self, epoch)

    def record_full_audit(runtime_dir: Path) -> None:
        timeline.append("audit")
        original_full_audit(runtime_dir)

    monkeypatch.setattr(
        CtpOrderSubmissionJournal,
        "_cleanup_sealed_epoch_sources",
        crash_first_cleanup,
    )
    monkeypatch.setattr(
        "afuture.cli._require_stress90_order_journal_full_audit",
        record_full_audit,
    )

    with pytest.raises(OSError, match="epoch cleanup crash"):
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason=reason,
            account_identity="c" * 64,
        )

    lifecycle = Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
    prepared = lifecycle.load_required()
    assert prepared.status == "prepared"
    retired_intent = intent_store.load_required_record()
    assert retired_intent.retired is True
    assert retired_intent.sequence == 2
    assert retired_intent.effective_account_identity_digest == "c" * 64
    assert retired_intent.effective_account_epoch == prepared.policy_target.live_account_epoch
    assert policy_store.load_required().live_account_identity_digest == "b" * 64
    assert (
        generic_store.load().strategy_states["directional_policy_identity"][
            "account_identity_digest"
        ]
        == "b" * 64
    )
    journal = CtpOrderSubmissionJournal(tmp_path / "stress90_ctp_orders.json")
    with pytest.raises(CtpOrderJournalIntegrityError, match="cleanup is incomplete"):
        journal.load_epoch_manifest()

    assert (
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason=reason,
            account_identity="c" * 64,
        )
        == 0
    )

    assert timeline == ["audit", "cleanup", "cleanup", "audit"]
    assert lifecycle.load_required().status == "committed"
    assert policy_store.load_required().live_account_identity_digest == "c" * 64
    assert (
        generic_store.load().strategy_states["directional_policy_identity"][
            "account_identity_digest"
        ]
        == "c" * 64
    )
    epoch_manifest = journal.load_epoch_manifest()
    assert epoch_manifest is not None
    assert epoch_manifest.pending_cleanup_epoch_id == ""
    assert epoch_manifest.current_account_identity_digest == "c" * 64
    assert journal.audit_epochs().sealed_epoch_count == 1
    assert intent_store.load_required_record().sequence == 2


@pytest.mark.parametrize(
    "retry_overrides",
    [
        {"operation_id": "e" * 64},
        {"operator_reason": "a different account-switch reason"},
        {"account_identity": "f" * 64},
    ],
    ids=("different-operation-id", "different-reason", "different-account"),
)
def test_account_switch_cleanup_crash_rejects_non_exact_lifecycle_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_overrides: dict[str, str],
) -> None:
    from afuture.broker.ctp_order_journal import CtpOrderSubmissionJournal
    from afuture.stress90_lifecycle_transaction import Stress90LifecycleTransactionStore

    _seeded_stress90_runtime(tmp_path, account_identity="b" * 64)
    reason = "switch account with one exact recoverable request"
    cleanup_attempts = 0
    original_cleanup = CtpOrderSubmissionJournal._cleanup_sealed_epoch_sources

    def crash_first_cleanup(self, epoch):
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise OSError("injected account-switch epoch cleanup crash")
        return original_cleanup(self, epoch)

    monkeypatch.setattr(
        CtpOrderSubmissionJournal,
        "_cleanup_sealed_epoch_sources",
        crash_first_cleanup,
    )
    with pytest.raises(OSError, match="epoch cleanup crash"):
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason=reason,
            account_identity="c" * 64,
        )

    retry = {
        "operator_reason": reason,
        "account_identity": "c" * 64,
        "operation_id": _OPERATION_NONCE,
    }
    retry.update(retry_overrides)
    with pytest.raises(RuntimeError):
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            **retry,
        )

    assert (
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
        .load_required()
        .status
        == "prepared"
    )


def test_account_rebase_rejects_a_backward_authoritative_trading_day(tmp_path: Path) -> None:
    from afuture.directional_stress90_state import (
        Stress90StateIntegrityError,
        rebase_stress90_account,
        record_completed_account_day,
    )

    _seed, _generic_store, _policy_store, _generic, policy = _seeded_stress90_runtime(tmp_path)
    completed = record_completed_account_day(policy.state, "20260826", 0.01)

    with pytest.raises(Stress90StateIntegrityError, match="(backward|trading day)"):
        rebase_stress90_account(
            completed,
            account_trading_day="20260825",
            account_equity=700_000.0,
            operator_reason="stale Broker snapshot",
            halted=True,
            broker_flat=True,
            local_flat=True,
            no_active_orders=True,
            reconciled=True,
            strong_confirmation="RESET_STRESS90_ACCOUNT_PATH",
            account_identity_digest="b" * 64,
            account_epoch=_OPERATION_NONCE,
        )


def test_account_rebase_transaction_cannot_mutate_candidate_state(tmp_path: Path) -> None:
    from afuture.directional_stress90_state import rebase_stress90_account
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    _seed, _generic_store, _policy_store, generic, policy = _seeded_stress90_runtime(tmp_path)
    rebased, _audit = rebase_stress90_account(
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
        account_epoch=_OPERATION_NONCE,
    )
    smuggled_candidate_change = replace(rebased, last_decision_digest="c" * 64)
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

    with pytest.raises(
        Stress90LifecycleTransactionError,
        match="(candidate|account rebase target|policy target transition)",
    ):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="account_rebase",
            generic_source=generic,
            policy_source=policy,
            generic_target=generic.state,
            policy_target=smuggled_candidate_change,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=account,
            operation_nonce=_OPERATION_NONCE,
            operator_reason="cash transfer",
        )


def test_account_rebase_transaction_binds_generic_target_to_exact_account_snapshot(
    tmp_path: Path,
) -> None:
    from afuture.directional_stress90_state import rebase_stress90_account
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    _seed, _generic_store, _policy_store, generic, policy = _seeded_stress90_runtime(tmp_path)
    rebased, _audit = rebase_stress90_account(
        policy.state,
        account_trading_day="20260825",
        account_equity=710_000.0,
        operator_reason="cash transfer",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        strong_confirmation="RESET_STRESS90_ACCOUNT_PATH",
        account_identity_digest="b" * 64,
        account_epoch=_OPERATION_NONCE,
    )
    account = AccountSnapshot(
        710_000.0,
        710_000.0,
        710_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
        withdrawal=1_000.0,
        previous_settlement_equity=700_000.0,
        settlement_verified=True,
        settlement_id=43,
    )

    with pytest.raises(
        Stress90LifecycleTransactionError,
        match="(account snapshot|generic account|account evidence).*(mismatch|transition)",
    ):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="account_rebase",
            generic_source=generic,
            policy_source=policy,
            # Deliberately retain the old 700k generic account values while the
            # exact Broker evidence and policy rebase both use 710k.
            generic_target=generic.state,
            policy_target=rebased,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=account,
            operation_nonce=_OPERATION_NONCE,
            operator_reason="cash transfer",
        )


def test_new_account_rebase_rejects_unverified_settlement_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seeded_stress90_runtime(tmp_path)

    with pytest.raises(RuntimeError, match="settlement.*unverified"):
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason="unverified settlement must not rebase",
            settlement_verified=False,
        )


def test_same_account_rebase_requires_verified_external_cash_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.directional_stress90_state import record_completed_account_day

    _seed, generic_store, policy_store, generic, policy = _seeded_stress90_runtime(tmp_path)
    policy_store.save(
        record_completed_account_day(policy.state, "20260825", -0.26),
        expected_sequence=policy.sequence,
    )
    generic_store.save(
        replace(
            generic.state,
            last_account_equity=518_000.0,
            equity_high_watermark=700_000.0,
        ),
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )

    with pytest.raises(
        RuntimeError,
        match="(cash flow|deposit|withdrawal|account change).*required",
    ):
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            equity=518_000.0,
            withdrawal=0.0,
            operator_reason="must not erase drawdown without an external cash flow",
        )


def test_explicit_account_switch_rebase_can_rebind_trading_day_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.trading_day_evidence import TradingDayEvidenceStore

    _seeded_stress90_runtime(tmp_path, account_identity="b" * 64)
    evidence_store = TradingDayEvidenceStore(tmp_path / "ctp_trading_day_evidence.json")

    assert (
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason="explicitly replace the commissioned account",
            account_identity="c" * 64,
            bypass_trading_day_checkpoint=False,
        )
        == 0
    )
    assert evidence_store.load_required().account_identity_digest == "c" * 64


def test_account_rebase_revokes_stale_daily_circuit_recovery_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed, generic_store, _policy_store, generic, _policy = _seeded_stress90_runtime(tmp_path)
    generic_store.save(
        replace(generic.state, directional_daily_circuit_day="20260824"),
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )

    assert (
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason="rebase must revoke circuit recovery authority",
        )
        == 0
    )
    rebased = generic_store.load()
    assert rebased.runtime_mode == RuntimeMode.HALTED.value
    assert rebased.kill_switch is True
    assert rebased.directional_daily_circuit_day == ""


def test_account_switch_rebase_does_not_seed_old_account_trade_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed, generic_store, _policy_store, generic, _policy = _seeded_stress90_runtime(
        tmp_path,
        account_identity="b" * 64,
    )
    old_trade_identity = "20260825:DCE:OLD-ACCOUNT-TRADE"
    generic_store.save(
        replace(
            generic.state,
            last_order_id="old-account-order",
            last_trade_id=old_trade_identity,
            recent_trade_ids=[old_trade_identity],
        ),
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )

    assert (
        _run_pending_rebase(
            tmp_path,
            monkeypatch,
            active_orders=[],
            operator_reason="switch to a different reconciled account",
            account_identity="c" * 64,
        )
        == 0
    )
    rebased = generic_store.load()
    assert rebased.last_order_id == ""
    assert rebased.last_trade_id == ""
    assert rebased.recent_trade_ids == []


def test_reactivation_after_migration_cannot_reuse_a_stale_account_path(
    tmp_path: Path,
) -> None:
    from afuture.directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        migrate_stress90_to_execution_aligned,
    )
    from afuture.directional_stress90_state import record_completed_account_day
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    seed, generic_store, policy_store, generic, policy = _seeded_stress90_runtime(tmp_path)
    completed_policy = record_completed_account_day(policy.state, "20260825", -0.10)
    policy = policy_store.save(completed_policy, expected_sequence=policy.sequence)
    migrated = migrate_stress90_to_execution_aligned(
        generic.state,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        account_identity_digest="b" * 64,
        operator_reason="run execution-aligned for two sessions",
        strong_confirmation=DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
    )
    migrated = replace(
        migrated,
        trading_day="20260827",
        day_start_equity=560_000.0,
        equity_high_watermark=700_000.0,
        last_account_equity=560_000.0,
        last_account_trading_day="20260827",
    )
    generic = generic_store.save(
        migrated,
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )
    reactivated = activate_stress90_policy(
        migrated,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=seed.seed_digest,
        account_identity_digest="b" * 64,
        operator_reason="reactivate after execution-aligned account-path gap",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    account = AccountSnapshot(
        560_000.0,
        560_000.0,
        560_000.0,
        0.0,
        0.0,
        0.0,
        "20260827",
        previous_settlement_equity=560_000.0,
        settlement_verified=True,
        settlement_id=45,
    )

    with pytest.raises(
        Stress90LifecycleTransactionError,
        match="(account path|account-day gap|rebase|fresh bootstrap)",
    ):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="activation",
            generic_source=generic,
            policy_source=policy,
            generic_target=reactivated,
            policy_target=policy.state,
            trading_day="20260827",
            account_identity_digest="b" * 64,
            account_snapshot=account,
            operation_nonce=_OPERATION_NONCE,
            operator_reason="reactivate after execution-aligned account-path gap",
        )


def test_reactivation_cannot_erase_an_unrecorded_completed_account_day(
    tmp_path: Path,
) -> None:
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    seed, _generic_store, _policy_store, generic, policy = _seeded_stress90_runtime(tmp_path)
    current_day_target = replace(
        generic.state,
        trading_day="20260827",
        day_start_equity=560_000.0,
        last_account_equity=560_000.0,
        last_account_trading_day="20260827",
    )
    current_day_target = activate_stress90_policy(
        current_day_target,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=seed.seed_digest,
        account_identity_digest="b" * 64,
        operator_reason="must not erase the offline account rollover",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    account = AccountSnapshot(
        560_000.0,
        560_000.0,
        560_000.0,
        0.0,
        0.0,
        0.0,
        "20260827",
        previous_settlement_equity=560_000.0,
        settlement_verified=True,
        settlement_id=45,
    )

    with pytest.raises(
        Stress90LifecycleTransactionError,
        match="(account path|account-day gap|rebase|rollover)",
    ):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="activation",
            generic_source=generic,
            policy_source=policy,
            generic_target=current_day_target,
            policy_target=policy.state,
            trading_day="20260827",
            account_identity_digest="b" * 64,
            account_snapshot=account,
            operation_nonce=_OPERATION_NONCE,
            operator_reason="must not erase the offline account rollover",
        )


def test_same_day_reactivation_cannot_reset_the_hard_daily_loss_baseline(
    tmp_path: Path,
) -> None:
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
    )

    seed, _generic_store, _policy_store, generic, policy = _seeded_stress90_runtime(tmp_path)
    reset_baseline = replace(
        generic.state,
        day_start_equity=650_000.0,
        last_account_equity=650_000.0,
    )
    reset_baseline = activate_stress90_policy(
        reset_baseline,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=seed.seed_digest,
        account_identity_digest="b" * 64,
        operator_reason="must not reset same-day daily-loss authority",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    account = AccountSnapshot(
        650_000.0,
        650_000.0,
        650_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
        previous_settlement_equity=650_000.0,
        settlement_verified=True,
        settlement_id=43,
    )

    with pytest.raises(
        Stress90LifecycleTransactionError,
        match="(daily.loss|day.start|account baseline|rebase)",
    ):
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json").begin(
            operation="activation",
            generic_source=generic,
            policy_source=policy,
            generic_target=reset_baseline,
            policy_target=policy.state,
            trading_day="20260825",
            account_identity_digest="b" * 64,
            account_snapshot=account,
            operation_nonce=_OPERATION_NONCE,
            operator_reason="must not reset same-day daily-loss authority",
        )
