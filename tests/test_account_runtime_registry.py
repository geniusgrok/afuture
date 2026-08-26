from __future__ import annotations

import json
from multiprocessing import get_context
from pathlib import Path

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
