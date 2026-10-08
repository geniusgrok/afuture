from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from afuture.models import ContractPosition, RuntimeMode
from afuture.state import RuntimeState, StateStore


def _source_state() -> RuntimeState:
    return RuntimeState(
        kill_switch=True,
        kill_reason="crash fill requires recovery",
        reconciled=False,
        metadata_verified=True,
        trading_day="20260825",
        positions=[],
        recent_trade_ids=[],
        runtime_mode=RuntimeMode.HALTED.value,
    )


def _write_cli_authority(runtime_dir: Path):
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
    from afuture.trading_day_evidence import TradingDayEvidenceStore

    account = "1" * 64
    epoch = "2" * 64
    bind_nonce = "5" * 64
    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260824",
        last_decision_digest="a" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "b" * 64},
        bootstrap_through_day="20260824",
        last_completed_input_day="20260821",
    )
    Stress90SeedStore(runtime_dir / "stress90_bootstrap_seed.json").save_new(seed)
    policy = bind_stress90_account_identity(
        Stress90PolicyState.from_seed(seed),
        account,
        account_epoch=epoch,
    )
    policy_record = Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json").save(
        policy
    )
    registry = AccountRuntimeRegistry(runtime_dir / ".account-runtime-registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    registry.bind_new(account, runtime_dir, epoch, bind_nonce)
    receipt = registry.require_binding_evidence(account, runtime_dir, epoch)
    generic = activate_stress90_policy(
        replace(_source_state(), reconciled=True),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=seed.seed_digest,
        account_identity_digest=account,
        risk_overlay_digest="e" * 64,
        operator_reason="bound crash-fill recovery fixture",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    generic = replace(generic, reconciled=False, metadata_verified=True)
    generic_record = StateStore(runtime_dir / "state.json").save(generic)
    evidence = TradingDayEvidenceStore(runtime_dir / "ctp_trading_day_evidence.json")._save(
        current=None,
        phase="bound",
        trading_day="20260825",
        account_identity_digest=account,
        account_epoch=epoch,
        canonical_runtime=receipt.binding.canonical_runtime,
        runtime_identity_digest=receipt.binding.runtime_identity_digest,
        account_binding_payload_digest=receipt.binding_payload_digest,
        account_binding_revision=receipt.binding_revision,
        account_binding_last_operation_id=receipt.binding.last_operation_id,
        account_binding_receipt_digest=receipt.binding_receipt_digest,
        registry_sequence=receipt.registry_sequence,
        registry_checksum=receipt.registry_checksum,
        rebind_transaction_id="6" * 64,
        previous_account_identity_digest=account,
    )
    return account, epoch, policy_record, generic_record, registry, evidence


def _cli_session_evidence():
    from afuture.broker.ctp_session_query import (
        CtpSessionTrade,
        build_ctp_session_activity_evidence,
    )
    from afuture.models import Offset, OrderSide

    trade = CtpSessionTrade(
        broker_id="broker",
        investor_id="investor",
        invest_unit_id="",
        trading_day="20260825",
        instrument_id="M2612",
        exchange_id="DCE",
        trade_id="T1",
        order_sys_id="SYS1",
        order_ref=1,
        side=OrderSide.BUY,
        offset=Offset.OPEN,
        volume=1,
        price=3_000.0,
        timestamp=datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc),
    )
    return build_ctp_session_activity_evidence(
        account_identity_digest="1" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=12,
        orders=(),
        trades=(trade,),
        critical_generation=7,
        query_ingress_generation=9,
    )


class _RecoveryBroker:
    send_count = 0
    cancel_count = 0
    fence_count = 0
    fence_hook = None
    active_orders: list[object] = []

    def __init__(self, _credentials) -> None:
        self.evidence = _cli_session_evidence()
        self.positions = [ContractPosition("M2612", "DCE", long_today=1, long_price=3_000.0)]

    @classmethod
    def reset(cls) -> None:
        cls.send_count = cls.cancel_count = cls.fence_count = 0
        cls.active_orders = []
        cls.fence_hook = None

    def get_account_identity_digest(self) -> str:
        return "1" * 64

    get_session_activity_account_identity_digest = get_account_identity_digest

    def configure_order_submission_journal(self, *_args, **_kwargs) -> None:
        return None

    def seed_trade_identities(self, _identities) -> None:
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    def snapshot_marker(self):
        return 1

    def snapshot_ready(self, _marker) -> bool:
        return True

    def get_trading_day(self) -> str:
        return "20260825"

    def get_account(self):
        from afuture.models import AccountSnapshot

        return AccountSnapshot(500_000.0, 500_000.0, 500_000.0, 0.0, 0.0, 0.0, "20260825")

    def get_positions(self):
        return list(self.positions)

    def get_active_orders(self):
        return list(type(self).active_orders)

    def get_contract_catalog(self):
        return []

    def require_session_activity_evidence_current(self, evidence) -> None:
        assert evidence.orders_digest == self.evidence.orders_digest
        assert evidence.trades_digest == self.evidence.trades_digest

    @contextmanager
    def lifecycle_state_commit_fence(self):
        type(self).fence_count += 1
        if type(self).fence_hook is not None:
            type(self).fence_hook()
        yield

    def send_order(self, _request):
        type(self).send_count += 1
        raise AssertionError("recovery must never send")

    def cancel_order(self, _order_id):
        type(self).cancel_count += 1
        raise AssertionError("recovery must never cancel")


def _cli_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from afuture.cli import _LifecycleMechanicalSnapshot
    from afuture.directional import DirectionalConfig
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.stress90_session_authority import Stress90SessionOwnershipProof

    account, epoch, policy, generic, registry, trading_evidence = _write_cli_authority(tmp_path)
    monkeypatch.setattr(
        "afuture.account_runtime_registry.PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH",
        tmp_path / ".account-runtime-registry.json",
    )
    _RecoveryBroker.reset()
    broker = _RecoveryBroker(object())

    def mechanical(current_broker, *, runtime_dir, timeout_seconds):
        del timeout_seconds
        from afuture.broker.ctp_session_query import CtpSessionActivityEvidenceStore

        assert current_broker is broker
        CtpSessionActivityEvidenceStore(runtime_dir / "stress90_ctp_session_evidence.json").save(
            broker.evidence
        )
        return _LifecycleMechanicalSnapshot(
            session_proof=Stress90SessionOwnershipProof(
                broker.evidence,
                "b" * 64,
                (),
            ),
            account=broker.get_account(),
            trading_day=broker.get_trading_day(),
            positions=tuple(broker.get_positions()),
            active_orders=tuple(broker.get_active_orders()),
            catalog=(),
        )

    def adopt(_config, current_broker, *, runtime_dir, state, broker_positions):
        del runtime_dir
        assert current_broker is broker
        return replace(
            state,
            positions=[asdict(item) for item in broker_positions],
            recent_trade_ids=[*state.recent_trade_ids, broker.evidence.trades[0].fill_key],
        )

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", lambda _credentials: broker)
    monkeypatch.setattr("afuture.cli._require_lifecycle_mechanical_snapshot", mechanical)
    monkeypatch.setattr("afuture.cli._adopt_stress90_lifecycle_crash_fills", adopt)
    config = type(
        "RecoveryConfig",
        (),
        {
            "mode": "live",
            "ctp": type("Credentials", (), {"environment": "test"})(),
            "directional": DirectionalConfig(
                enabled=True,
                policy="stress90",
                products=FROZEN_PRODUCTS,
                account_exclusive=True,
            ),
            "state_path": str(tmp_path / "state.json"),
            "account_registry_path": str(tmp_path / ".account-runtime-registry.json"),
            "journal_path": str(tmp_path / "audit.jsonl"),
        },
    )()
    args = type(
        "Args",
        (),
        {
            "confirm_live": True,
            "confirm_recovery": True,
            "operation_id": "a" * 64,
            "operator_reason": "verified authorized CTP crash fill",
            "runtime_dir": "",
            "startup_timeout": 1.0,
            "snapshot_wait": 0.1,
        },
    )()
    return config, args, broker, policy, generic, registry, trading_evidence


def test_recovery_cli_commits_under_fence_exact_retry_and_sends_zero_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_crash_fill_recovery
    from afuture.stress90_crash_fill_recovery import (
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
        Stress90CrashFillRecoveryStore,
    )

    config, args, _broker, _policy, source, registry, _evidence = _cli_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv(
        "AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK",
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    assert _run_stress90_crash_fill_recovery(config, args) == 0
    first = StateStore(tmp_path / "state.json").load_required_record()
    checkpoint = Stress90CrashFillRecoveryStore(
        tmp_path / "stress90_crash_fill_recovery.json"
    ).load_required_record()
    assert first.sequence == source.sequence + 1
    assert checkpoint.checkpoint.status == "committed"
    assert checkpoint.checkpoint.adopted_fill_ids == ("20260825:DCE:CTP.T1",)
    receipt = registry.require_binding_evidence("1" * 64, tmp_path, "2" * 64)
    assert receipt.binding.last_operation_id == args.operation_id
    assert receipt.binding.operation_kinds[-1] == "stress90_crash_fill_recovery"
    trading_day = (
        __import__("afuture.trading_day_evidence", fromlist=["TradingDayEvidenceStore"])
        .TradingDayEvidenceStore(tmp_path / "ctp_trading_day_evidence.json")
        .load_required()
    )
    assert trading_day.account_binding_receipt_digest == receipt.binding_receipt_digest
    assert checkpoint.checkpoint.trading_day_evidence == trading_day
    assert _RecoveryBroker.fence_count == 1
    assert _RecoveryBroker.send_count == _RecoveryBroker.cancel_count == 0

    assert _run_stress90_crash_fill_recovery(config, args) == 0
    assert StateStore(tmp_path / "state.json").load_required_record() == first
    assert _RecoveryBroker.fence_count == 2
    assert _RecoveryBroker.send_count == _RecoveryBroker.cancel_count == 0


def test_recovery_cli_rejects_unknown_session_and_generic_cas_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_crash_fill_recovery
    from afuture.stress90_crash_fill_recovery import (
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    config, args, _broker, _policy, _source, _registry, _evidence = _cli_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv(
        "AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK",
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )
    monkeypatch.setattr(
        "afuture.cli._require_lifecycle_mechanical_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("unknown session trade blocks recovery")
        ),
    )
    with pytest.raises(RuntimeError, match="unknown session trade"):
        _run_stress90_crash_fill_recovery(config, args)

    cas_runtime = tmp_path / "cas"
    cas_runtime.mkdir()
    config, args, _broker, _policy, source, _registry, _evidence = _cli_fixture(
        cas_runtime, monkeypatch
    )
    original_save = StateStore.save
    injected = False

    def conflict(self, state, **kwargs):
        nonlocal injected
        if kwargs.get("expected_sequence") == source.sequence and not injected:
            injected = True
            original_save(self, replace(source.state, kill_reason="concurrent mutation"))
        return original_save(self, state, **kwargs)

    monkeypatch.setattr(StateStore, "save", conflict)
    with pytest.raises(RuntimeError, match="concurrent|unrelated"):
        _run_stress90_crash_fill_recovery(config, args)
    assert _RecoveryBroker.send_count == _RecoveryBroker.cancel_count == 0


def test_recovery_cli_rejects_epoch_mismatch_prepared_lifecycle_and_runtime_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_crash_fill_recovery
    from afuture.directional_stress90_state import Stress90PolicyStateStore
    from afuture.stress90_crash_fill_recovery import (
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    config, args, _broker, policy, _source, _registry, _evidence = _cli_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv(
        "AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK",
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )
    Stress90PolicyStateStore(tmp_path / "stress90_policy_state.json").save(
        replace(policy.state, live_account_epoch="d" * 64),
        expected_sequence=policy.sequence,
    )
    with pytest.raises(RuntimeError, match="epoch|lineage|binding"):
        _run_stress90_crash_fill_recovery(config, args)

    prepared_runtime = tmp_path / "prepared"
    prepared_runtime.mkdir()
    config, args, _broker, _policy, _source, _registry, _evidence = _cli_fixture(
        prepared_runtime, monkeypatch
    )
    monkeypatch.setattr(
        "afuture.stress90_lifecycle_transaction.Stress90LifecycleTransactionStore.load",
        lambda _self: type("Prepared", (), {"status": "prepared"})(),
    )
    with pytest.raises(RuntimeError, match="prepared lifecycle"):
        _run_stress90_crash_fill_recovery(config, args)

    alias_runtime = tmp_path / "alias"
    alias_runtime.mkdir()
    args.runtime_dir = str(alias_runtime)
    with pytest.raises(RuntimeError, match="canonical runtime"):
        _run_stress90_crash_fill_recovery(config, args)
    assert _RecoveryBroker.send_count == _RecoveryBroker.cancel_count == 0
