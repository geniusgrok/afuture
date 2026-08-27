from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
from multiprocessing import get_context
from pathlib import Path

import pytest

from afuture.models import ContractPosition, RuntimeMode
from afuture.state import RuntimeState, StateStore


def _session_evidence(*, trades=()):
    from afuture.broker.ctp_session_query import build_ctp_session_activity_evidence

    return build_ctp_session_activity_evidence(
        account_identity_digest="1" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=12,
        orders=(),
        trades=tuple(trades),
        critical_generation=7,
        query_ingress_generation=9,
    )


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


def _target_state() -> RuntimeState:
    return replace(
        _source_state(),
        kill_reason="Stress-90 crash fills recovered; lifecycle gates remain required",
        reconciled=True,
        metadata_verified=False,
        positions=[
            {
                "symbol": "M2612",
                "exchange": "DCE",
                "long_today": 1,
                "long_yesterday": 0,
                "short_today": 0,
                "short_yesterday": 0,
                "long_price": 3_000.0,
                "short_price": 0.0,
            }
        ],
        recent_trade_ids=["20260825:DCE:T-RECOVERED"],
        strategy_states={},
    )


def _checkpoint(
    runtime_dir: Path,
    *,
    operation_nonce: str = "a" * 64,
    operator_reason: str = "adopt exact authorized crash fill",
):
    from afuture.stress90_crash_fill_recovery import (
        Stress90CrashFillRecoveryAuthority,
        build_stress90_crash_fill_recovery_checkpoint,
    )

    source_record = StateStore(runtime_dir / "state.json").load_required_record()
    prior = source_record.state.positions[0] if source_record.state.positions else None
    next_volume = 1 if prior is None else int(prior["long_today"]) + 1
    fill_id = f"20260825:DCE:T-RECOVERED-{operation_nonce[:8]}"
    target = replace(
        source_record.state,
        kill_reason="Stress-90 crash fills recovered; lifecycle gates remain required",
        reconciled=True,
        metadata_verified=False,
        positions=[
            {
                "symbol": "M2612",
                "exchange": "DCE",
                "long_today": next_volume,
                "long_yesterday": 0,
                "short_today": 0,
                "short_yesterday": 0,
                "long_price": 3_000.0,
                "short_price": 0.0,
            }
        ],
        recent_trade_ids=[*source_record.state.recent_trade_ids, fill_id],
    )
    return build_stress90_crash_fill_recovery_checkpoint(
        operation_nonce=operation_nonce,
        operator_reason=operator_reason,
        authority=Stress90CrashFillRecoveryAuthority(
            account_identity_digest="1" * 64,
            account_epoch="2" * 64,
            canonical_runtime=str(runtime_dir.resolve()),
            runtime_identity_digest="3" * 64,
            account_binding_payload_digest="4" * 64,
            account_binding_revision=1,
            account_binding_last_operation_id="5" * 64,
            account_binding_receipt_digest="6" * 64,
            registry_sequence=7,
            registry_checksum="7" * 64,
            policy_state_sequence=8,
            policy_state_checksum="8" * 64,
        ),
        trading_day="20260825",
        session_evidence=_session_evidence(),
        session_evidence_sequence=3,
        session_evidence_checksum="9" * 64,
        session_ownership_digest="b" * 64,
        generic_source=source_record,
        generic_target=target,
        source_positions_digest="c" * 64,
        target_positions_digest="d" * 64,
        adopted_fill_ids=(fill_id,),
    )


def _begin_checkpoint_process(path: str, runtime_dir: str, nonce: str, start, results) -> None:
    from afuture.stress90_crash_fill_recovery import (
        Stress90CrashFillRecoveryError,
        Stress90CrashFillRecoveryStore,
    )

    start.wait(timeout=10)
    try:
        record = Stress90CrashFillRecoveryStore(path).begin(
            _checkpoint(Path(runtime_dir), operation_nonce=nonce)
        )
    except Stress90CrashFillRecoveryError:
        results.put("rejected")
    else:
        results.put(f"prepared:{record.checkpoint.operation_nonce}")


def test_crash_fill_recovery_parser_is_distinct_and_strongly_confirmed() -> None:
    from afuture.cli import build_parser

    args = build_parser().parse_args(
        [
            "stress90-crash-fill-recover",
            "--config",
            "live.toml",
            "--confirm-live",
            "--confirm-recovery",
            "--operation-id",
            "a" * 64,
            "--operator-reason",
            "verified authorized CTP crash fills",
        ]
    )

    assert args.command == "stress90-crash-fill-recover"
    assert args.confirm_recovery is True
    assert args.operation_id == "a" * 64
    assert args.operator_reason == "verified authorized CTP crash fills"


def test_recovery_store_read_only_load_is_pristine_and_does_not_create_lineage(
    tmp_path: Path,
) -> None:
    from afuture.stress90_crash_fill_recovery import Stress90CrashFillRecoveryStore

    store = Stress90CrashFillRecoveryStore(tmp_path / "stress90_crash_fill_recovery.json")

    assert store.load_record() is None
    assert not store.path.exists()
    assert not store.previous_path.exists()
    assert not store.lineage_path.exists()
    assert not store.lock_path.exists()


def test_recovery_store_prepare_commit_chain_and_prev_is_evidence_only(tmp_path: Path) -> None:
    from afuture.stress90_crash_fill_recovery import Stress90CrashFillRecoveryStore

    StateStore(tmp_path / "state.json").save(_source_state())
    store = Stress90CrashFillRecoveryStore(tmp_path / "stress90_crash_fill_recovery.json")
    prepared = store.begin(_checkpoint(tmp_path))
    committed = store.mark_committed(prepared.checkpoint.transaction_id)

    assert prepared.sequence == 1
    assert prepared.checkpoint.status == "prepared"
    assert committed.sequence == 2
    assert committed.parent_checksum == prepared.checksum
    assert committed.checkpoint.status == "committed"
    assert store.load_required_record() == committed
    assert store.load_previous_record() == prepared
    assert store.lineage_path.is_file()
    assert store.lock_path.is_file()

    store.path.write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="current.*recovery"):
        store.load_required_record()
    assert store.load_previous_record() == prepared


@pytest.mark.parametrize("survivor", ["lineage", "previous", "lock"])
def test_recovery_store_missing_current_never_rebuilds_surviving_lineage(
    tmp_path: Path,
    survivor: str,
) -> None:
    from afuture.stress90_crash_fill_recovery import (
        Stress90CrashFillRecoveryError,
        Stress90CrashFillRecoveryStore,
    )

    StateStore(tmp_path / "state.json").save(_source_state())
    store = Stress90CrashFillRecoveryStore(tmp_path / "stress90_crash_fill_recovery.json")
    prepared = store.begin(_checkpoint(tmp_path))
    store.mark_committed(prepared.checkpoint.transaction_id)
    store.path.unlink()
    for name, path in {
        "lineage": store.lineage_path,
        "previous": store.previous_path,
        "lock": store.lock_path,
    }.items():
        if name != survivor and path.exists():
            path.unlink()

    with pytest.raises(Stress90CrashFillRecoveryError, match="missing.*evidence"):
        store.load_record()
    with pytest.raises(Stress90CrashFillRecoveryError, match="missing.*evidence"):
        store.begin(_checkpoint(tmp_path, operation_nonce="b" * 64))


def test_recovery_nonce_history_is_permanent_and_exact_retry_only(tmp_path: Path) -> None:
    from afuture.stress90_crash_fill_recovery import (
        Stress90CrashFillRecoveryError,
        Stress90CrashFillRecoveryStore,
    )

    state_store = StateStore(tmp_path / "state.json")
    state_store.save(_source_state())
    store = Stress90CrashFillRecoveryStore(tmp_path / "stress90_crash_fill_recovery.json")
    first = store.begin(_checkpoint(tmp_path))
    committed = store.mark_committed(first.checkpoint.transaction_id)

    assert store.begin(_checkpoint(tmp_path)) == committed
    with pytest.raises(Stress90CrashFillRecoveryError, match="nonce.*different"):
        store.begin(_checkpoint(tmp_path, operator_reason="changed evidence"))

    for index in range(1, 7):
        nonce = f"{index:064x}"
        checkpoint = _checkpoint(tmp_path, operation_nonce=nonce)
        prepared = store.begin(checkpoint)
        store.mark_committed(prepared.checkpoint.transaction_id)

    assert len(store.load_required_record().operation_history) == 7
    with pytest.raises(Stress90CrashFillRecoveryError, match="already consumed"):
        store.begin(_checkpoint(tmp_path))


def test_recovery_store_rejects_a_checksummed_truncated_nonce_history(tmp_path: Path) -> None:
    from afuture.stress90_crash_fill_recovery import (
        Stress90CrashFillRecoveryError,
        Stress90CrashFillRecoveryStore,
    )

    StateStore(tmp_path / "state.json").save(_source_state())
    store = Stress90CrashFillRecoveryStore(tmp_path / "stress90_crash_fill_recovery.json")
    first = store.begin(_checkpoint(tmp_path))
    store.mark_committed(first.checkpoint.transaction_id)
    second = store.begin(_checkpoint(tmp_path, operation_nonce="b" * 64))
    store.mark_committed(second.checkpoint.transaction_id)

    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    envelope["operation_history"] = envelope["operation_history"][-1:]
    unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
    envelope["checksum"] = sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    store.path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(Stress90CrashFillRecoveryError, match="history.*transition"):
        store.load_required_record()


def test_recovery_store_aliases_share_interprocess_cas(tmp_path: Path) -> None:
    StateStore(tmp_path / "state.json").save(_source_state())
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_dir, target_is_directory=True)
    path = real_dir / "stress90_crash_fill_recovery.json"
    alias_path = alias / ".." / "alias" / "stress90_crash_fill_recovery.json"
    context = get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_begin_checkpoint_process,
            args=(str(candidate), str(tmp_path), nonce, start, results),
        )
        for candidate, nonce in ((path, "a" * 64), (alias_path, "b" * 64))
    ]
    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert sorted(item.split(":", 1)[0] for item in outcomes) == ["prepared", "rejected"]


def test_recovery_transaction_rolls_forward_both_crash_boundaries_exactly_once(
    tmp_path: Path,
) -> None:
    from afuture.stress90_crash_fill_recovery import (
        Stress90CrashFillRecoveryError,
        Stress90CrashFillRecoveryStore,
        apply_stress90_crash_fill_recovery,
    )

    state_store = StateStore(tmp_path / "state.json")
    source = state_store.save(_source_state())
    checkpoint_store = Stress90CrashFillRecoveryStore(
        tmp_path / "stress90_crash_fill_recovery.json"
    )
    checkpoint = _checkpoint(tmp_path)

    # Crash after durable checkpoint prepare, before generic state CAS.
    checkpoint_store.begin(checkpoint)
    completed = apply_stress90_crash_fill_recovery(checkpoint_store, state_store)
    target = state_store.load_required_record()
    assert completed.checkpoint.status == "committed"
    assert target.sequence == source.sequence + 1
    assert target.state == checkpoint.generic_target

    # Exact retry does not advance either participant.
    assert apply_stress90_crash_fill_recovery(checkpoint_store, state_store) == completed
    assert state_store.load_required_record() == target

    # A second transaction crashes after generic state CAS but before checkpoint commit.
    second = _checkpoint(tmp_path, operation_nonce="b" * 64)
    second_prepared = checkpoint_store.begin(second)
    state_store.save(
        second_prepared.checkpoint.generic_target,
        expected_sequence=second_prepared.checkpoint.generic_source_sequence,
        expected_checksum=second_prepared.checkpoint.generic_source_checksum,
    )
    second_completed = apply_stress90_crash_fill_recovery(checkpoint_store, state_store)
    assert second_completed.checkpoint.status == "committed"

    state_store.save(replace(_target_state(), kill_reason="unrelated revision"))
    with pytest.raises(Stress90CrashFillRecoveryError, match="unrelated"):
        apply_stress90_crash_fill_recovery(checkpoint_store, state_store)


def test_lifecycle_consumes_only_exact_committed_recovery_proof(tmp_path: Path) -> None:
    from afuture.stress90_crash_fill_recovery import (
        Stress90CrashFillRecoveryStore,
        apply_stress90_crash_fill_recovery,
        require_committed_stress90_crash_fill_recovery,
    )

    state_store = StateStore(tmp_path / "state.json")
    state_store.save(_source_state())
    recovery_store = Stress90CrashFillRecoveryStore(tmp_path / "stress90_crash_fill_recovery.json")
    recovery_store.begin(_checkpoint(tmp_path))
    apply_stress90_crash_fill_recovery(recovery_store, state_store)
    current = state_store.load_required_record()

    require_committed_stress90_crash_fill_recovery(recovery_store, current)
    with pytest.raises(RuntimeError, match="exact generic state"):
        require_committed_stress90_crash_fill_recovery(
            recovery_store,
            replace(current, checksum="0" * 64),
        )


def test_recovery_target_preserves_halted_kill_switch_and_never_needs_cancellation() -> None:
    target = _target_state()
    positions = [ContractPosition(**item) for item in target.positions]

    assert target.runtime_mode == RuntimeMode.HALTED.value
    assert target.kill_switch is True
    assert target.reconciled is True
    assert target.metadata_verified is False
    assert positions[0].long_total == 1


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

    config, args, _broker, _policy, source, _registry, _evidence = _cli_fixture(
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
    assert _RecoveryBroker.fence_count == 1
    assert _RecoveryBroker.send_count == _RecoveryBroker.cancel_count == 0

    assert _run_stress90_crash_fill_recovery(config, args) == 0
    assert StateStore(tmp_path / "state.json").load_required_record() == first
    assert _RecoveryBroker.fence_count == 2
    assert _RecoveryBroker.send_count == _RecoveryBroker.cancel_count == 0


def test_recovery_cli_treats_other_account_registry_revision_as_audit_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistry
    from afuture.cli import _run_stress90_crash_fill_recovery
    from afuture.stress90_crash_fill_recovery import (
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    config, args, _broker, _policy, _source, registry, _evidence = _cli_fixture(
        tmp_path, monkeypatch
    )
    other_runtime = tmp_path / "other-account-runtime"
    other_runtime.mkdir()

    def interleave_other_account() -> None:
        assert isinstance(registry, AccountRuntimeRegistry)
        registry.bind_new("7" * 64, other_runtime, "8" * 64, "9" * 64)
        _RecoveryBroker.fence_hook = None

    _RecoveryBroker.fence_hook = interleave_other_account
    monkeypatch.setenv(
        "AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK",
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    assert _run_stress90_crash_fill_recovery(config, args) == 0
    assert _RecoveryBroker.send_count == _RecoveryBroker.cancel_count == 0


@pytest.mark.parametrize(
    ("state_change", "active", "expected"),
    [
        ({"runtime_mode": RuntimeMode.RUNNING.value}, False, "HALTED"),
        ({"kill_switch": False}, False, "kill switch"),
        ({}, True, "active orders"),
    ],
)
def test_recovery_cli_rejects_wrong_runtime_gates_without_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_change: dict[str, object],
    active: bool,
    expected: str,
) -> None:
    from afuture.cli import _run_stress90_crash_fill_recovery
    from afuture.models import Offset, Order, OrderRequest, OrderSide, OrderStatus
    from afuture.stress90_crash_fill_recovery import (
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    config, args, _broker, _policy, source, _registry, _evidence = _cli_fixture(
        tmp_path, monkeypatch
    )
    if state_change:
        StateStore(tmp_path / "state.json").save(
            replace(source.state, **state_change),
            expected_sequence=source.sequence,
            expected_checksum=source.checksum,
        )
    if active:
        _RecoveryBroker.active_orders = [
            Order(
                "CTP.active",
                OrderRequest(
                    "M2612",
                    "DCE",
                    OrderSide.BUY,
                    Offset.OPEN,
                    volume=1,
                    price=3_000.0,
                ),
                OrderStatus.NOT_TRADED,
            )
        ]
    monkeypatch.setenv(
        "AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK",
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    with pytest.raises(RuntimeError, match=expected):
        _run_stress90_crash_fill_recovery(config, args)
    assert _RecoveryBroker.send_count == _RecoveryBroker.cancel_count == 0


def test_recovery_cli_requires_strong_confirmation_and_rejects_changed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_crash_fill_recovery
    from afuture.stress90_crash_fill_recovery import (
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    config, args, _broker, _policy, _source, registry, _evidence = _cli_fixture(
        tmp_path, monkeypatch
    )
    with pytest.raises(RuntimeError, match="strong confirmation"):
        _run_stress90_crash_fill_recovery(config, args)

    monkeypatch.setenv(
        "AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK",
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )
    registry.acknowledge_binding_operation(
        "1" * 64,
        tmp_path,
        "2" * 64,
        "c" * 64,
    )
    with pytest.raises(RuntimeError, match="trading-day evidence.*authority"):
        _run_stress90_crash_fill_recovery(config, args)
    assert _RecoveryBroker.fence_count == 0


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


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("mode", "system.mode=live"),
        ("policy", "explicit stress90"),
    ],
)
def test_recovery_cli_rejects_wrong_mode_and_non_stress90(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected: str,
) -> None:
    from afuture.cli import _run_stress90_crash_fill_recovery
    from afuture.directional import DirectionalConfig
    from afuture.stress90_crash_fill_recovery import (
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    config, args, _broker, _policy, _source, _registry, _evidence = _cli_fixture(
        tmp_path, monkeypatch
    )
    if mutation == "mode":
        config.mode = "replay"
    else:
        config.directional = DirectionalConfig(
            enabled=True,
            policy="execution_aligned",
            products=config.directional.products,
            account_exclusive=True,
        )
    monkeypatch.setenv(
        "AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK",
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    with pytest.raises(ValueError, match=expected):
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


def test_recovery_cli_rejects_concurrent_account_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_crash_fill_recovery
    from afuture.runtime_lease import AccountExclusiveRuntimeLease, RuntimeLeaseError
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
    with AccountExclusiveRuntimeLease(tmp_path, "1" * 64, role="concurrent-owner"):
        with pytest.raises(RuntimeLeaseError, match="already owned"):
            _run_stress90_crash_fill_recovery(config, args)
    assert _RecoveryBroker.send_count == _RecoveryBroker.cancel_count == 0


def test_recovery_cli_exact_nonce_rejects_changed_session_evidence_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.broker.ctp_session_query import build_ctp_session_activity_evidence
    from afuture.cli import _run_stress90_crash_fill_recovery
    from afuture.stress90_crash_fill_recovery import (
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )

    config, args, broker, _policy, _source, _registry, _evidence = _cli_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv(
        "AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK",
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
    )
    assert _run_stress90_crash_fill_recovery(config, args) == 0
    changed_trade = replace(broker.evidence.trades[0], price=3_001.0)
    broker.evidence = build_ctp_session_activity_evidence(
        account_identity_digest="1" * 64,
        trading_day="20260825",
        order_request_id=21,
        trade_request_id=22,
        orders=(),
        trades=(changed_trade,),
        critical_generation=8,
        query_ingress_generation=10,
    )
    with pytest.raises(RuntimeError, match="changed evidence"):
        _run_stress90_crash_fill_recovery(config, args)

    state_runtime = tmp_path / "state-change"
    state_runtime.mkdir()
    config, args, _broker, _policy, _source, _registry, _evidence = _cli_fixture(
        state_runtime, monkeypatch
    )
    assert _run_stress90_crash_fill_recovery(config, args) == 0
    current = StateStore(state_runtime / "state.json").load_required_record()
    StateStore(state_runtime / "state.json").save(
        replace(current.state, kill_reason="unrelated operator state edit"),
        expected_sequence=current.sequence,
        expected_checksum=current.checksum,
    )
    with pytest.raises(RuntimeError, match="unrelated|exact generic"):
        _run_stress90_crash_fill_recovery(config, args)
    assert _RecoveryBroker.send_count == _RecoveryBroker.cancel_count == 0


def test_lifecycle_guard_consumes_exact_committed_checkpoint_marker(tmp_path: Path) -> None:
    from afuture.cli import _require_no_unpersisted_lifecycle_crash_fill_adoption
    from afuture.stress90_crash_fill_recovery import (
        Stress90CrashFillRecoveryStore,
        apply_stress90_crash_fill_recovery,
    )

    state_store = StateStore(tmp_path / "state.json")
    state_store.save(_source_state())
    recovery_store = Stress90CrashFillRecoveryStore(tmp_path / "stress90_crash_fill_recovery.json")
    recovery_store.begin(_checkpoint(tmp_path))
    apply_stress90_crash_fill_recovery(recovery_store, state_store)
    current = state_store.load_required_record()

    _require_no_unpersisted_lifecycle_crash_fill_adoption(
        current.state,
        current.state,
        pending_lifecycle=None,
        persisted_record=current,
        runtime_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="not exact and committed"):
        _require_no_unpersisted_lifecycle_crash_fill_adoption(
            current.state,
            current.state,
            pending_lifecycle=None,
            persisted_record=replace(current, checksum="0" * 64),
            runtime_dir=tmp_path,
        )
