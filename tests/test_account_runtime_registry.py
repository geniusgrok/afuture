from __future__ import annotations

import fcntl
import json
import os
import stat
from multiprocessing import get_context
from pathlib import Path
from threading import BrokenBarrierError

import pytest


def _initialized_registry(path: Path):
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )

    registry = AccountRuntimeRegistry(path)
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    return registry


def test_registry_initialization_cli_requires_explicit_operator_gates() -> None:
    from afuture.cli import build_parser

    args = build_parser().parse_args(
        [
            "stress90-registry-init",
            "--config",
            "live.toml",
            "--confirm-initialize",
            "--operator-reason",
            "provision fixed machine anchor",
        ]
    )

    assert args.command == "stress90-registry-init"
    assert args.confirm_initialize is True
    assert args.operator_reason == "provision fixed machine anchor"


def _claim_account_runtime(
    registry_path: str,
    account: str,
    runtime_dir: str,
    epoch: str,
    operation: str,
    start,
    results,
) -> None:
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    start.wait(timeout=10)
    try:
        AccountRuntimeRegistry(registry_path).bind_new(
            account,
            runtime_dir,
            epoch,
            operation,
        )
    except AccountRuntimeRegistryError:
        results.put("rejected")
    else:
        results.put("bound")


def _block_inside_pristine_registry_load(
    registry_path: str,
    entered,
    release,
) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistry

    registry = AccountRuntimeRegistry(registry_path)

    def blocked_load(*, required: bool, legacy_lock_evidence: bool):
        assert required is False
        assert legacy_lock_evidence is False
        entered.set()
        release.wait(timeout=30)
        return None

    registry._load_unlocked = blocked_load  # type: ignore[method-assign]
    registry.load()


def _bind_account_through_alias_with_save_barrier(
    registry_path: str,
    account: str,
    runtime_dir: str,
    epoch: str,
    operation: str,
    save_barrier,
    results,
) -> None:
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    registry = AccountRuntimeRegistry(registry_path)
    real_save = registry._save_unlocked

    def synchronized_save(previous, bindings):
        try:
            save_barrier.wait(timeout=3)
        except BrokenBarrierError:
            pass
        return real_save(previous, bindings)

    registry._save_unlocked = synchronized_save  # type: ignore[method-assign]
    try:
        registry.bind_new(account, runtime_dir, epoch, operation)
    except AccountRuntimeRegistryError:
        results.put("rejected")
    else:
        results.put("bound")


def test_account_binding_survives_release_and_rejects_another_runtime(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    path = tmp_path / "machine" / "account-runtime-registry.json"
    account = "a" * 64
    epoch = "b" * 64
    operation = "c" * 64
    runtime_a = tmp_path / "runtime-a"
    runtime_b = tmp_path / "runtime-b"

    _initialized_registry(path)
    first = AccountRuntimeRegistry(path).bind_new(
        account,
        runtime_a,
        epoch,
        operation,
    )
    exact_retry = AccountRuntimeRegistry(path).bind_new(
        account,
        runtime_a,
        epoch,
        operation,
    )

    assert exact_retry == first
    assert exact_retry.sequence == 2
    assert AccountRuntimeRegistry(path).require_binding(
        account, runtime_a, epoch
    ).account_epoch == (epoch)
    with pytest.raises(AccountRuntimeRegistryError, match="different runtime"):
        AccountRuntimeRegistry(path).bind_new(
            account,
            runtime_b,
            "d" * 64,
            "e" * 64,
        )


def test_account_epoch_advance_is_exact_cas_and_idempotent(tmp_path: Path) -> None:
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistryError,
    )

    registry = _initialized_registry(tmp_path / "registry.json")
    account = "1" * 64
    source_epoch = "2" * 64
    target_epoch = "3" * 64
    operation = "4" * 64
    runtime = tmp_path / "runtime"
    registry.bind_new(account, runtime, source_epoch, "5" * 64)

    advanced = registry.advance_epoch(
        account,
        runtime,
        source_epoch,
        target_epoch,
        operation,
    )
    retry = registry.advance_epoch(
        account,
        runtime,
        source_epoch,
        target_epoch,
        operation,
    )

    assert retry == advanced
    assert retry.sequence == 3
    assert registry.require_binding(account, runtime, target_epoch).account_epoch == target_epoch
    with pytest.raises(AccountRuntimeRegistryError, match="epoch CAS"):
        registry.advance_epoch(
            account,
            runtime,
            "6" * 64,
            "7" * 64,
            "8" * 64,
        )


def test_account_switch_atomically_retires_source_lineage(tmp_path: Path) -> None:
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistryError,
    )

    registry = _initialized_registry(tmp_path / "registry.json")
    source_account = "1" * 64
    target_account = "2" * 64
    source_epoch = "3" * 64
    target_epoch = "4" * 64
    operation = "5" * 64
    runtime = tmp_path / "runtime"
    registry.bind_new(source_account, runtime, source_epoch, "6" * 64)

    switched = registry.switch_account_binding(
        source_account_identity_digest=source_account,
        target_account_identity_digest=target_account,
        runtime_dir=runtime,
        source_epoch=source_epoch,
        target_epoch=target_epoch,
        operation_id=operation,
    )
    retry = registry.switch_account_binding(
        source_account_identity_digest=source_account,
        target_account_identity_digest=target_account,
        runtime_dir=runtime,
        source_epoch=source_epoch,
        target_epoch=target_epoch,
        operation_id=operation,
    )

    assert retry == switched
    assert retry.sequence == 3
    assert registry.require_binding(target_account, runtime, target_epoch).account_epoch == (
        target_epoch
    )
    with pytest.raises(AccountRuntimeRegistryError, match="binding is missing"):
        registry.require_binding(source_account, runtime, source_epoch)
    with pytest.raises(AccountRuntimeRegistryError, match="retired"):
        registry.bind_new(source_account, runtime, source_epoch, "7" * 64)


def test_duplicate_current_previous_crash_layout_is_exactly_retryable(
    tmp_path: Path,
) -> None:
    registry = _initialized_registry(tmp_path / "registry.json")
    account = "a" * 64
    runtime = tmp_path / "runtime"
    registry.bind_new(account, runtime, "b" * 64, "c" * 64)
    registry.advance_epoch(account, runtime, "b" * 64, "d" * 64, "e" * 64)

    # Crash after copying current N to .prev but before replacing current with N+1.
    registry.previous_path.write_bytes(registry.path.read_bytes())

    assert registry.load_required().sequence == 3
    advanced = registry.advance_epoch(
        account,
        runtime,
        "d" * 64,
        "f" * 64,
        "0" * 64,
    )
    assert advanced.sequence == 4


def test_registry_current_corruption_never_falls_back_to_previous(tmp_path: Path) -> None:
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistryError,
    )

    path = tmp_path / "registry.json"
    registry = _initialized_registry(path)
    account = "a" * 64
    runtime = tmp_path / "runtime"
    registry.bind_new(account, runtime, "b" * 64, "c" * 64)
    registry.advance_epoch(account, runtime, "b" * 64, "d" * 64, "e" * 64)
    assert registry.previous_path.exists()
    path.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(AccountRuntimeRegistryError, match="invalid JSON"):
        registry.load_required()


def test_registry_rejects_missing_current_when_previous_exists(tmp_path: Path) -> None:
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    path = tmp_path / "registry.json"
    registry = AccountRuntimeRegistry(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    registry.previous_path.write_text(json.dumps({"evidence": "not current"}), encoding="utf-8")

    with pytest.raises(AccountRuntimeRegistryError, match="current.*missing"):
        registry.load()


def test_missing_registry_requires_explicit_machine_initialization(tmp_path: Path) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    registry = AccountRuntimeRegistry(tmp_path / "registry.json")
    with pytest.raises(AccountRuntimeRegistryError, match="required.*missing"):
        registry.bind_new("a" * 64, tmp_path / "runtime", "b" * 64, "c" * 64)
    with pytest.raises(AccountRuntimeRegistryError, match="initialization confirmation"):
        registry.initialize(strong_confirmation="wrong")

    initialized = registry.initialize(
        strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION
    )
    retry = registry.initialize(
        strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION
    )

    assert retry == initialized
    assert retry.sequence == 1
    assert retry.bindings == ()


def test_registry_requires_absolute_canonical_runtime(tmp_path: Path) -> None:
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistryError,
    )

    registry = _initialized_registry(tmp_path / "registry.json")

    with pytest.raises(AccountRuntimeRegistryError, match="absolute canonical"):
        registry.bind_new("a" * 64, Path("relative/runtime"), "b" * 64, "c" * 64)


def test_account_runtime_transfer_requires_all_gates_and_preserves_lineage(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_TRANSFER_CONFIRMATION,
        AccountRuntimeRegistryError,
    )

    registry = _initialized_registry(tmp_path / "registry.json")
    account = "a" * 64
    source_epoch = "b" * 64
    target_epoch = "c" * 64
    operation = "d" * 64
    runtime_a = tmp_path / "runtime-a"
    runtime_b = tmp_path / "runtime-b"
    registry.bind_new(account, runtime_a, source_epoch, "e" * 64)
    kwargs = {
        "account_identity_digest": account,
        "source_runtime_dir": runtime_a,
        "target_runtime_dir": runtime_b,
        "source_epoch": source_epoch,
        "target_epoch": target_epoch,
        "operation_id": operation,
        "operator_reason": "explicit halted canonical runtime transfer",
        "halted": True,
        "broker_flat": True,
        "local_flat": True,
        "no_active_orders": True,
        "reconciled": True,
        "strong_confirmation": ACCOUNT_RUNTIME_TRANSFER_CONFIRMATION,
    }

    with pytest.raises(AccountRuntimeRegistryError, match="safety gates"):
        registry.transfer_binding(**{**kwargs, "broker_flat": False})
    assert registry.load_required().sequence == 2

    transferred = registry.transfer_binding(**kwargs)
    retry = registry.transfer_binding(**kwargs)
    binding = registry.require_binding(account, runtime_b, target_epoch)

    assert retry == transferred
    assert retry.sequence == 3
    assert binding.runtime_identity_digest != binding.retired_runtime_identity_digests[-1]
    with pytest.raises(AccountRuntimeRegistryError, match="different runtime"):
        registry.bind_new(account, runtime_a, source_epoch, "f" * 64)


def test_concurrent_processes_cannot_claim_one_account_for_two_runtimes(
    tmp_path: Path,
) -> None:
    context = get_context("spawn")
    start = context.Event()
    results = context.Queue()
    path = tmp_path / "registry.json"
    account = "a" * 64
    _initialized_registry(path)
    first = context.Process(
        target=_claim_account_runtime,
        args=(
            str(path),
            account,
            str(tmp_path / "runtime-a"),
            "b" * 64,
            "c" * 64,
            start,
            results,
        ),
    )
    second = context.Process(
        target=_claim_account_runtime,
        args=(
            str(path),
            account,
            str(tmp_path / "runtime-b"),
            "d" * 64,
            "e" * 64,
            start,
            results,
        ),
    )
    first.start()
    second.start()
    start.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert sorted((results.get(timeout=2), results.get(timeout=2))) == [
        "bound",
        "rejected",
    ]


def test_registry_path_identity_normalizes_lexical_and_parent_symlink_aliases(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistry

    real_parent = tmp_path / "real"
    nested = real_parent / "nested"
    nested.mkdir(parents=True)
    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(real_parent, target_is_directory=True)
    canonical_path = real_parent / "registry.json"

    canonical = AccountRuntimeRegistry(canonical_path)
    lexical_alias = AccountRuntimeRegistry(nested / ".." / "registry.json")
    symlink_alias = AccountRuntimeRegistry(parent_alias / "registry.json")

    assert canonical.path == canonical_path
    assert lexical_alias.path == canonical_path
    assert symlink_alias.path == canonical_path
    assert lexical_alias.lock_path == canonical.lock_path
    assert symlink_alias.lineage_path == canonical.lineage_path


def test_alias_writers_share_one_lock_after_visible_lock_is_removed(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistry

    context = get_context("spawn")
    results = context.Queue()
    save_barrier = context.Barrier(2)
    parent = tmp_path / "registry-parent"
    nested = parent / "nested"
    nested.mkdir(parents=True)
    canonical_path = parent / "registry.json"
    lexical_alias_path = nested / ".." / "registry.json"
    registry = _initialized_registry(canonical_path)
    registry.lock_path.unlink()
    account = "a" * 64

    first = context.Process(
        target=_bind_account_through_alias_with_save_barrier,
        args=(
            str(lexical_alias_path),
            account,
            str(tmp_path / "runtime-a"),
            "b" * 64,
            "c" * 64,
            save_barrier,
            results,
        ),
    )
    second = context.Process(
        target=_bind_account_through_alias_with_save_barrier,
        args=(
            str(canonical_path),
            account,
            str(tmp_path / "runtime-b"),
            "d" * 64,
            "e" * 64,
            save_barrier,
            results,
        ),
    )
    first.start()
    second.start()
    first.join(timeout=15)
    second.join(timeout=15)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert sorted((results.get(timeout=2), results.get(timeout=2))) == [
        "bound",
        "rejected",
    ]
    final = AccountRuntimeRegistry(canonical_path).load_required()
    previous = json.loads(registry.previous_path.read_text(encoding="utf-8"))
    assert final.sequence == 2
    assert previous["sequence"] == 1
    assert final.parent_checksum == previous["checksum"]


def test_visible_lock_symlink_replacement_fails_closed(tmp_path: Path) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _initialized_registry(tmp_path / "registry.json")
    target = tmp_path / "lock-target"
    target.write_text("do not modify", encoding="utf-8")
    registry.lock_path.unlink()
    registry.lock_path.symlink_to(target)

    with pytest.raises(AccountRuntimeRegistryError, match="lock failed"):
        registry.load_required()

    assert target.read_text(encoding="utf-8") == "do not modify"


def test_visible_lock_eexist_race_is_flocked_until_registry_lock_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _initialized_registry(tmp_path / "registry.json")
    registry.lock_path.unlink()
    real_exists = registry._exists
    injected = False

    def create_lock_during_initial_check(path: Path) -> bool:
        nonlocal injected
        if path == registry.lock_path and not injected:
            injected = True
            path.touch(mode=0o600)
            return False
        return real_exists(path)

    monkeypatch.setattr(registry, "_exists", create_lock_during_initial_check)
    with registry._exclusive_lock() as lock:
        assert lock.legacy_lock_evidence is False
        assert lock.visible_descriptor is not None
        contender = os.open(registry.lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)

    contender = os.open(registry.lock_path, os.O_RDWR)
    try:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(contender)


def test_registry_lock_setup_failure_releases_kernel_lock_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    registry = AccountRuntimeRegistry(tmp_path / "registry.json")
    kernel_descriptor = os.open("/dev/null", os.O_RDWR)
    monkeypatch.setattr(
        registry,
        "_acquire_kernel_lock",
        lambda _path: kernel_descriptor,
    )

    def fail_exists(_path: Path) -> bool:
        raise AccountRuntimeRegistryError("injected evidence check failure")

    monkeypatch.setattr(registry, "_exists", fail_exists)
    with pytest.raises(AccountRuntimeRegistryError, match="evidence check"):
        with registry._exclusive_lock():
            pytest.fail("lock body must not be entered")

    with pytest.raises(OSError):
        os.fstat(kernel_descriptor)


def test_visible_lock_cleanup_failure_does_not_mask_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _initialized_registry(tmp_path / "registry.json")
    registry.lock_path.unlink()
    real_fsync = os.fsync
    real_close = os.close
    failed_descriptor: int | None = None

    def fail_visible_lock_fsync(descriptor: int) -> None:
        nonlocal failed_descriptor
        descriptor_path = Path(f"/proc/self/fd/{descriptor}").resolve()
        if descriptor_path == registry.lock_path:
            failed_descriptor = descriptor
            raise OSError("injected visible lock fsync failure")
        real_fsync(descriptor)

    def fail_matching_close(descriptor: int) -> None:
        real_close(descriptor)
        if descriptor == failed_descriptor:
            raise OSError("injected visible lock close failure")

    monkeypatch.setattr(os, "fsync", fail_visible_lock_fsync)
    monkeypatch.setattr(os, "close", fail_matching_close)
    with pytest.raises(AccountRuntimeRegistryError, match="lock failed") as exc_info:
        registry.load_required()

    assert exc_info.value.__cause__ is not None
    assert "fsync failure" in str(exc_info.value.__cause__)


def test_registry_lineage_marker_prevents_deleted_anchor_reinitialization(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistryError,
    )

    registry = _initialized_registry(tmp_path / "registry.json")
    account = "a" * 64
    runtime = tmp_path / "runtime"
    registry.bind_new(account, runtime, "b" * 64, "c" * 64)

    assert registry.lineage_path.is_file()
    registry.path.unlink()
    registry.previous_path.unlink()

    for operation in (
        registry.load,
        registry.load_required,
        lambda: registry.initialize(
            strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION
        ),
        lambda: registry.bind_new(account, tmp_path / "other-runtime", "d" * 64, "e" * 64),
    ):
        with pytest.raises(AccountRuntimeRegistryError, match="lineage.*missing"):
            operation()


@pytest.mark.parametrize("failure_target", ["marker", "parent"])
def test_registry_marker_fsync_failure_never_accepts_an_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    registry = AccountRuntimeRegistry(tmp_path / "registry.json")
    real_fsync = os.fsync

    def fail_selected_fsync(descriptor: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if (failure_target == "parent") is is_directory:
            raise OSError(f"injected {failure_target} fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_selected_fsync)
    with pytest.raises(AccountRuntimeRegistryError, match="lineage marker"):
        registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)

    assert registry.lineage_path.exists()
    assert not registry.path.exists()
    monkeypatch.setattr(os, "fsync", real_fsync)
    with pytest.raises(AccountRuntimeRegistryError, match="lineage.*missing"):
        registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)


def test_registry_acknowledges_unchanged_binding_operation_exactly_once(
    tmp_path: Path,
) -> None:
    registry = _initialized_registry(tmp_path / "registry.json")
    account = "a" * 64
    epoch = "b" * 64
    bind_operation = "c" * 64
    acknowledgement = "d" * 64
    runtime = tmp_path / "runtime"
    registry.bind_new(account, runtime, epoch, bind_operation)
    before = registry.require_binding(account, runtime, epoch)

    acknowledged = registry.acknowledge_binding_operation(
        account,
        runtime,
        epoch,
        acknowledgement,
    )
    exact_retry = registry.acknowledge_binding_operation(
        account,
        runtime,
        epoch,
        acknowledgement,
    )
    after = registry.require_binding(account, runtime, epoch)

    assert acknowledged.sequence == 3
    assert exact_retry == acknowledged
    assert after.account_identity_digest == before.account_identity_digest
    assert after.canonical_runtime == before.canonical_runtime
    assert after.runtime_identity_digest == before.runtime_identity_digest
    assert after.account_epoch == before.account_epoch
    assert after.retired_runtime_identity_digests == before.retired_runtime_identity_digests
    assert after.retired_account_identity_digests == before.retired_account_identity_digests
    assert after.retired_lineage_digests == before.retired_lineage_digests
    assert after.operation_history == (*before.operation_history, acknowledgement)
    assert after.last_operation_id == acknowledgement
    next_epoch = "e" * 64
    registry.advance_epoch(account, runtime, epoch, next_epoch, "f" * 64)
    assert (
        acknowledgement in registry.require_binding(account, runtime, next_epoch).operation_history
    )


def test_registry_unchanged_binding_acknowledgement_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture import account_runtime_registry as registry_module
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _initialized_registry(tmp_path / "registry.json")
    first_account = "1" * 64
    second_account = "2" * 64
    epoch = "3" * 64
    runtime_a = tmp_path / "runtime-a"
    runtime_b = tmp_path / "runtime-b"
    registry.bind_new(first_account, runtime_a, epoch, "4" * 64)
    registry.bind_new(second_account, runtime_b, epoch, "5" * 64)

    with pytest.raises(AccountRuntimeRegistryError, match="already consumed"):
        registry.acknowledge_binding_operation(second_account, runtime_b, epoch, "5" * 64)

    with pytest.raises(AccountRuntimeRegistryError, match="source CAS"):
        registry.acknowledge_binding_operation(
            first_account,
            runtime_a,
            "6" * 64,
            "7" * 64,
        )

    consumed = "8" * 64
    registry.acknowledge_binding_operation(first_account, runtime_a, epoch, consumed)
    with pytest.raises(AccountRuntimeRegistryError, match="already consumed"):
        registry.acknowledge_binding_operation(second_account, runtime_b, epoch, consumed)

    binding = registry.require_binding(first_account, runtime_a, epoch)
    monkeypatch.setattr(
        registry_module,
        "_MAX_OPERATION_HISTORY",
        len(binding.operation_history),
    )
    with pytest.raises(AccountRuntimeRegistryError, match="history exhausted"):
        registry.acknowledge_binding_operation(
            first_account,
            runtime_a,
            epoch,
            "9" * 64,
        )


def test_surviving_legacy_lock_prevents_missing_registry_reinitialization(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistryError,
    )

    registry = _initialized_registry(tmp_path / "registry.json")
    registry.bind_new("a" * 64, tmp_path / "runtime-a", "b" * 64, "c" * 64)
    registry.path.unlink()
    registry.previous_path.unlink()
    registry.lineage_path.unlink()
    assert registry.lock_path.exists()

    with pytest.raises(AccountRuntimeRegistryError, match="lock.*lineage"):
        registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    with pytest.raises(AccountRuntimeRegistryError, match="lock.*lineage"):
        registry.bind_new("a" * 64, tmp_path / "runtime-b", "d" * 64, "e" * 64)
    assert not registry.path.exists()


def test_pristine_registry_read_does_not_leave_false_lineage(tmp_path: Path) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )

    registry = AccountRuntimeRegistry(tmp_path / "registry.json")

    assert registry.load() is None
    assert not registry.lock_path.exists()
    initialized = registry.initialize(
        strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION
    )

    assert initialized.sequence == 1
    assert registry.lock_path.exists()


def test_killed_pristine_registry_read_never_leaves_visible_lineage_lock(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )

    context = get_context("spawn")
    entered = context.Event()
    release = context.Event()
    registry = AccountRuntimeRegistry(tmp_path / "registry.json")
    process = context.Process(
        target=_block_inside_pristine_registry_load,
        args=(str(registry.path), entered, release),
    )
    process.start()
    assert entered.wait(timeout=10)

    process.terminate()
    process.join(timeout=10)

    assert process.exitcode is not None
    assert not registry.lock_path.exists()
    initialized = registry.initialize(
        strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION
    )
    assert initialized.sequence == 1


def test_valid_legacy_registry_records_upgrade_lineage_once(tmp_path: Path) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistry

    sequence_one = _initialized_registry(tmp_path / "sequence-one.json")
    sequence_one.lineage_path.unlink()
    upgraded_one = AccountRuntimeRegistry(sequence_one.path).load_required()
    assert upgraded_one.sequence == 1
    assert sequence_one.lineage_path.is_file()

    chained = _initialized_registry(tmp_path / "chained.json")
    chained.bind_new("a" * 64, tmp_path / "runtime", "b" * 64, "c" * 64)
    chained.advance_epoch(
        "a" * 64,
        tmp_path / "runtime",
        "b" * 64,
        "d" * 64,
        "e" * 64,
    )
    chained.lineage_path.unlink()
    upgraded_chain = AccountRuntimeRegistry(chained.path).load_required()
    assert upgraded_chain.sequence == 3
    assert chained.lineage_path.is_file()

    duplicate = _initialized_registry(tmp_path / "duplicate.json")
    duplicate.bind_new("1" * 64, tmp_path / "duplicate-runtime", "2" * 64, "3" * 64)
    duplicate.advance_epoch(
        "1" * 64,
        tmp_path / "duplicate-runtime",
        "2" * 64,
        "4" * 64,
        "5" * 64,
    )
    duplicate.previous_path.write_bytes(duplicate.path.read_bytes())
    duplicate.lineage_path.unlink()
    upgraded_duplicate = AccountRuntimeRegistry(duplicate.path).load_required()
    assert upgraded_duplicate.sequence == 3
    assert duplicate.lineage_path.is_file()


def test_invalid_legacy_registry_chain_never_upgrades_lineage(tmp_path: Path) -> None:
    from afuture import account_runtime_registry as registry_module
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    registry = _initialized_registry(tmp_path / "registry.json")
    runtime = tmp_path / "runtime"
    registry.bind_new("a" * 64, runtime, "b" * 64, "c" * 64)
    registry.advance_epoch("a" * 64, runtime, "b" * 64, "d" * 64, "e" * 64)
    registry.lineage_path.unlink()
    previous = json.loads(registry.previous_path.read_text(encoding="utf-8"))
    previous["parent_checksum"] = "f" * 64
    unsigned = {key: value for key, value in previous.items() if key != "checksum"}
    previous["checksum"] = registry_module._digest(unsigned)
    registry.previous_path.write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(AccountRuntimeRegistryError, match="parent chain"):
        AccountRuntimeRegistry(registry.path).load_required()
    assert not registry.lineage_path.exists()


@pytest.mark.parametrize("failure_target", ["marker", "parent"])
def test_legacy_registry_upgrade_fsync_failure_does_not_return_a_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    from afuture.account_runtime_registry import (
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    registry = _initialized_registry(tmp_path / "registry.json")
    original = registry.path.read_bytes()
    registry.lineage_path.unlink()
    real_fsync = os.fsync

    def fail_selected_fsync(descriptor: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if (failure_target == "parent") is is_directory:
            raise OSError(f"injected legacy upgrade {failure_target} fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_selected_fsync)
    with pytest.raises(AccountRuntimeRegistryError, match="lineage marker"):
        AccountRuntimeRegistry(registry.path).load_required()

    assert registry.path.read_bytes() == original
    assert registry.lineage_path.exists()
    retried_targets: list[str] = []

    def record_retry_fsync(descriptor: int) -> None:
        retried_targets.append("parent" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "marker")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_retry_fsync)
    assert AccountRuntimeRegistry(registry.path).load_required().sequence == 1
    assert {"marker", "parent"}.issubset(retried_targets)


@pytest.mark.parametrize("failure_target", ["marker", "parent"])
def test_existing_registry_marker_refsync_failure_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _initialized_registry(tmp_path / "registry.json")
    real_fsync = os.fsync

    def fail_selected_fsync(descriptor: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if (failure_target == "parent") is is_directory:
            raise OSError(f"injected existing marker {failure_target} fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_selected_fsync)
    with pytest.raises(AccountRuntimeRegistryError, match="lineage marker.*durability"):
        registry.load_required()
