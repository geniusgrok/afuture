from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest


def _registry(path: Path):
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )

    registry = AccountRuntimeRegistry(path)
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    return registry


def _ledger(registry):
    from afuture.account_runtime_nonce_ledger import AccountRuntimeNonceLedger

    return AccountRuntimeNonceLedger.for_registry(registry.path)


def _binding_inputs(tmp_path: Path) -> tuple[str, Path, str]:
    return "1" * 64, tmp_path / "runtime", "2" * 64


def test_pristine_registry_anchors_ready_empty_authenticated_nonce_dictionary(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_nonce_ledger import EMPTY_NONCE_ROOT

    registry = _registry(tmp_path / "registry.json")
    record = registry.load_required()

    assert record.nonce_root == EMPTY_NONCE_ROOT
    assert record.nonce_count == 0
    assert _ledger(registry).load_ready_root() == (EMPTY_NONCE_ROOT, 0)


def test_legacy_nonce_migration_artifact_fails_closed_without_rewrite(tmp_path: Path) -> None:
    """A current registry never treats a migration witness as current evidence."""
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _registry(tmp_path / "registry.json")
    ledger = _ledger(registry)
    legacy_artifact = ledger.directory / "migration.json"
    legacy_artifact.write_text('{"legacy": true}', encoding="utf-8")
    current = registry.path.read_bytes()

    with pytest.raises(AccountRuntimeRegistryError, match="nonce ledger integrity"):
        registry.load_required()

    assert registry.path.read_bytes() == current
    assert legacy_artifact.read_text(encoding="utf-8") == '{"legacy": true}'


def test_pristine_initialize_rejects_migration_artifact_without_any_write(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    registry = AccountRuntimeRegistry(tmp_path / "registry.json")
    ledger = _ledger(registry)
    ledger.directory.mkdir()
    (ledger.directory / "migration.json").write_text('{"legacy": true}', encoding="utf-8")

    def evidence() -> dict[str, tuple[str, bytes]]:
        return {
            str(path.relative_to(tmp_path)): (
                "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
                b"" if path.is_dir() or path.is_symlink() else path.read_bytes(),
            )
            for path in tmp_path.rglob("*")
        }

    before = evidence()

    with pytest.raises(AccountRuntimeRegistryError, match="migration artifact"):
        registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)

    assert evidence() == before
    assert not registry.lineage_path.exists()
    assert not registry.lock_path.exists()


def test_pristine_initialize_rechecks_migration_artifact_inside_exclusive_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
        AccountRuntimeRegistryError,
    )

    registry = AccountRuntimeRegistry(tmp_path / "registry.json")
    ledger = _ledger(registry)
    real_exclusive_lock = registry._exclusive_lock
    evidence_after_injection: dict[str, tuple[str, bytes]] = {}

    def evidence() -> dict[str, tuple[str, bytes]]:
        return {
            str(path.relative_to(tmp_path)): (
                "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
                b"" if path.is_dir() or path.is_symlink() else path.read_bytes(),
            )
            for path in tmp_path.rglob("*")
        }

    @contextmanager
    def inject_migration_artifact_inside_lock():
        with real_exclusive_lock() as lock:
            ledger.directory.mkdir()
            (ledger.directory / "migration.json").write_text('{"legacy": true}', encoding="utf-8")
            evidence_after_injection.update(evidence())
            yield lock

    monkeypatch.setattr(registry, "_exclusive_lock", inject_migration_artifact_inside_lock)

    with pytest.raises(AccountRuntimeRegistryError, match="migration artifact"):
        registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)

    assert evidence() == evidence_after_injection
    assert not registry.lineage_path.exists()
    assert not registry.lock_path.exists()


def test_legacy_migration_artifact_blocks_direct_nonce_ledger_apis_without_writes(
    tmp_path: Path,
) -> None:
    """Direct ledger calls cannot bypass current-only artifact rejection."""
    from afuture.account_runtime_nonce_ledger import (
        EMPTY_NONCE_ROOT,
        AccountRuntimeNonceLedgerError,
        build_nonce_receipt,
    )

    registry = _registry(tmp_path / "registry.json")
    ledger = _ledger(registry)
    (ledger.directory / "migration.json").write_text('{"legacy": true}', encoding="utf-8")
    receipt = build_nonce_receipt(
        operation_nonce="1" * 64,
        operation_kind="bind",
        account_identity_digest="2" * 64,
        canonical_runtime=str(tmp_path / "runtime"),
        runtime_identity_digest="3" * 64,
        account_epoch="4" * 64,
        semantic_request_digest="5" * 64,
    )

    def evidence() -> dict[str, bytes]:
        return {
            str(path.relative_to(ledger.directory)): path.read_bytes()
            for path in ledger.directory.rglob("*")
            if path.is_file()
        }

    before = evidence()
    for operation in (
        lambda: ledger.lookup(EMPTY_NONCE_ROOT, receipt.operation_nonce),
        lambda: ledger.require_receipt(EMPTY_NONCE_ROOT, receipt.operation_nonce),
        lambda: ledger.membership_node_paths(EMPTY_NONCE_ROOT, receipt.operation_nonce),
        lambda: ledger.load_transition(1),
        lambda: ledger.create_transition({}),
        lambda: ledger.insert(old_root=EMPTY_NONCE_ROOT, old_count=0, receipt=receipt),
        ledger.largest_receipt_size,
        ledger.largest_node_size,
    ):
        with pytest.raises(AccountRuntimeNonceLedgerError, match="migration artifact"):
            operation()

    assert evidence() == before


@pytest.mark.parametrize(
    "first_kind",
    ["bind", "advance", "acknowledgement", "recovery", "switch", "transfer"],
)
def test_every_registry_nonce_api_shares_one_cross_kind_membership_index(
    tmp_path: Path,
    first_kind: str,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_TRANSFER_CONFIRMATION,
        AccountRuntimeRegistryError,
    )

    registry = _registry(tmp_path / f"{first_kind}.json")
    account, runtime, epoch = _binding_inputs(tmp_path / first_kind)
    seed = "a" * 64
    nonce = "b" * 64
    registry.bind_new(account, runtime, epoch, seed)
    final_account = account
    final_runtime = runtime
    final_epoch = epoch

    if first_kind == "bind":
        other = "3" * 64
        final_account, final_runtime, final_epoch = other, tmp_path / first_kind / "other", "4" * 64
        registry.bind_new(final_account, final_runtime, final_epoch, nonce)
    elif first_kind == "advance":
        final_epoch = "3" * 64
        registry.advance_epoch(account, runtime, epoch, final_epoch, nonce)
    elif first_kind == "acknowledgement":
        registry.acknowledge_binding_operation(account, runtime, epoch, nonce)
    elif first_kind == "recovery":
        registry.acknowledge_stress90_recovery_operation(
            account,
            runtime,
            epoch,
            nonce,
            "c" * 64,
        )
    elif first_kind == "switch":
        final_account, final_epoch = "3" * 64, "4" * 64
        registry.switch_account_binding(
            source_account_identity_digest=account,
            target_account_identity_digest=final_account,
            runtime_dir=runtime,
            source_epoch=epoch,
            target_epoch=final_epoch,
            operation_id=nonce,
        )
    else:
        final_runtime, final_epoch = tmp_path / first_kind / "target", "4" * 64
        registry.transfer_binding(
            account_identity_digest=account,
            source_runtime_dir=runtime,
            target_runtime_dir=final_runtime,
            source_epoch=epoch,
            target_epoch=final_epoch,
            operation_id=nonce,
            operator_reason="move exact halted lineage",
            halted=True,
            broker_flat=True,
            local_flat=True,
            no_active_orders=True,
            reconciled=True,
            strong_confirmation=ACCOUNT_RUNTIME_TRANSFER_CONFIRMATION,
        )

    receipt = _ledger(registry).require_receipt(registry.load_required().nonce_root, nonce)
    assert receipt.operation_kind == (
        "stress90_crash_fill_recovery" if first_kind == "recovery" else first_kind
    )
    with pytest.raises(AccountRuntimeRegistryError, match="already consumed|different request"):
        if first_kind == "acknowledgement":
            registry.acknowledge_stress90_recovery_operation(
                final_account,
                final_runtime,
                final_epoch,
                nonce,
                "d" * 64,
            )
        else:
            registry.acknowledge_binding_operation(
                final_account,
                final_runtime,
                final_epoch,
                nonce,
            )


@pytest.mark.parametrize("deleted", ["receipt", "path_node"])
def test_deleting_historical_authenticated_nonce_evidence_fails_closed(
    tmp_path: Path,
    deleted: str,
) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _registry(tmp_path / "registry.json")
    account, runtime, epoch = _binding_inputs(tmp_path)
    nonce = "a" * 64
    registry.bind_new(account, runtime, epoch, nonce)
    ledger = _ledger(registry)
    anchored = registry.load_required()
    if deleted == "receipt":
        ledger.receipt_path(nonce).unlink()
    else:
        ledger.membership_node_paths(anchored.nonce_root, nonce)[0].unlink()

    with pytest.raises(AccountRuntimeRegistryError, match="nonce|ledger|integrity|missing"):
        registry.bind_new(account, runtime, epoch, nonce)
    with pytest.raises(AccountRuntimeRegistryError, match="nonce|ledger|integrity|missing"):
        registry.require_binding(account, runtime, epoch)


def _resign_registry(path: Path, mutate) -> str:
    from afuture import account_runtime_registry as registry_module

    envelope = json.loads(path.read_text(encoding="utf-8"))
    mutate(envelope)
    unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
    envelope["checksum"] = registry_module._digest(unsigned)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return envelope["checksum"]


def test_resigning_current_and_previous_cannot_reuse_an_old_nonce(tmp_path: Path) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _registry(tmp_path / "registry.json")
    account, runtime, epoch = _binding_inputs(tmp_path)
    bind_nonce = "a" * 64
    consumed = "b" * 64
    target_epoch = "3" * 64
    registry.bind_new(account, runtime, epoch, bind_nonce)
    registry.acknowledge_binding_operation(account, runtime, epoch, consumed)
    registry.advance_epoch(account, runtime, epoch, target_epoch, "c" * 64)

    def truncate_current(raw: dict[str, object]) -> None:
        binding = raw["bindings"][0]  # type: ignore[index]
        binding["operation_history"] = [bind_nonce, "c" * 64]
        binding["operation_kinds"] = ["bind", "advance"]

    def truncate_previous(raw: dict[str, object]) -> None:
        from afuture import account_runtime_registry as registry_module

        binding = raw["bindings"][0]  # type: ignore[index]
        binding["last_operation_id"] = bind_nonce
        binding["operation_history"] = [bind_nonce]
        binding["operation_kinds"] = ["bind"]
        binding["last_operation_receipt_digest"] = registry_module._operation_receipt(
            "bind",
            operation_id=bind_nonce,
            account_identity_digest=account,
            canonical_runtime=str(runtime.resolve()),
            account_epoch=epoch,
        )

    previous_checksum = _resign_registry(registry.previous_path, truncate_previous)

    def truncate_linked_current(raw: dict[str, object]) -> None:
        truncate_current(raw)
        raw["parent_checksum"] = previous_checksum

    _resign_registry(registry.path, truncate_linked_current)
    with pytest.raises(AccountRuntimeRegistryError, match="nonce|ledger|consumed|anchor"):
        registry.acknowledge_binding_operation(account, runtime, target_epoch, consumed)


def test_nonce_lookup_is_bounded_without_directory_scan_and_cap_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture import account_runtime_nonce_ledger as ledger_module
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _registry(tmp_path / "registry.json")
    account, runtime, epoch = _binding_inputs(tmp_path)
    registry.bind_new(account, runtime, epoch, "1" * 64)
    for index in range(1, 1_002):
        registry.acknowledge_binding_operation(account, runtime, epoch, f"{index + 1:064x}")

    anchored = registry.load_required()
    assert anchored.nonce_count == 1_002
    assert registry.path.stat().st_size < 64_000
    ledger = _ledger(registry)
    assert ledger.largest_receipt_size() < 16_000
    assert ledger.largest_node_size() < 4_000

    def reject_scan(*_args, **_kwargs):
        raise AssertionError("normal nonce proof must not enumerate a directory")

    monkeypatch.setattr(os, "scandir", reject_scan)
    assert ledger.require_receipt(anchored.nonce_root, f"{1_002:064x}").operation_nonce

    monkeypatch.setattr(ledger_module, "MAX_NONCE_RECEIPTS", anchored.nonce_count)
    with pytest.raises(AccountRuntimeRegistryError, match="capacity|cap"):
        registry.acknowledge_binding_operation(account, runtime, epoch, "f" * 64)
    assert registry.acknowledge_binding_operation(
        account,
        runtime,
        epoch,
        f"{1_002:064x}",
    )


def test_removed_nonce_migration_cli_is_rejected_while_current_registry_init_remains() -> None:
    from afuture.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "stress90-registry-nonce-migrate",
                "--config",
                "live.toml",
                "--confirm-live",
                "--confirm-nonce-migration",
                "--operator-reason",
                "obsolete historical migration",
            ]
        )
    current = parser.parse_args(
        [
            "stress90-registry-init",
            "--config",
            "live.toml",
            "--confirm-initialize",
            "--operator-reason",
            "new current registry",
        ]
    )
    assert current.command == "stress90-registry-init"
    rebase = parser.parse_args(
        [
            "stress90-account-rebase",
            "--config",
            "live.toml",
            "--confirm-live",
            "--confirm-rebase",
            "--operator-reason",
            "current account lifecycle rebase",
            "--operation-id",
            "a" * 64,
        ]
    )
    assert rebase.command == "stress90-account-rebase"


def test_crash_after_receipt_before_registry_cas_rolls_forward_only_exact_request(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _registry(tmp_path / "registry.json")
    account, runtime, epoch = _binding_inputs(tmp_path)
    registry.bind_new(account, runtime, epoch, "a" * 64)
    nonce = "b" * 64
    registry._nonce_commit_fault = "after_receipt_before_registry"  # type: ignore[attr-defined]
    with pytest.raises(AccountRuntimeRegistryError, match="injected|nonce"):
        registry.acknowledge_binding_operation(account, runtime, epoch, nonce)
    del registry._nonce_commit_fault  # type: ignore[attr-defined]

    exact = registry.acknowledge_binding_operation(account, runtime, epoch, nonce)
    assert _ledger(registry).require_receipt(exact.nonce_root, nonce).operation_kind == (
        "acknowledgement"
    )
    with pytest.raises(AccountRuntimeRegistryError, match="different|consumed"):
        registry.acknowledge_stress90_recovery_operation(
            account,
            runtime,
            epoch,
            nonce,
            "c" * 64,
        )


def test_immutable_nonce_head_rejects_rollback_to_valid_authenticated_subset(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _registry(tmp_path / "registry.json")
    initial_bytes = registry.path.read_bytes()
    account, runtime, epoch = _binding_inputs(tmp_path)
    registry.bind_new(account, runtime, epoch, "a" * 64)
    subset_bytes = registry.path.read_bytes()
    omitted_nonce = "b" * 64
    registry.acknowledge_binding_operation(account, runtime, epoch, omitted_nonce)

    # Both restored records and the old nonce root are independently valid, but
    # immutable count-2 transition evidence proves this is a signed rollback.
    registry.previous_path.write_bytes(initial_bytes)
    registry.path.write_bytes(subset_bytes)
    with pytest.raises(AccountRuntimeRegistryError, match="rollback|head|transition|nonce"):
        registry.acknowledge_binding_operation(account, runtime, epoch, omitted_nonce)


def test_exact_retry_reconciles_pending_after_registry_cas_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path / "registry.json")
    account, runtime, epoch = _binding_inputs(tmp_path)
    registry.bind_new(account, runtime, epoch, "a" * 64)
    ledger = _ledger(registry)
    operation = "b" * 64
    original_unlink = Path.unlink
    failed = False

    def fail_pending_cleanup(path: Path, *args, **kwargs) -> None:
        nonlocal failed
        if path == ledger.pending_path and not failed:
            failed = True
            raise OSError("injected pending unlink ambiguity")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_pending_cleanup)
    with pytest.raises(OSError, match="pending unlink"):
        registry.acknowledge_binding_operation(account, runtime, epoch, operation)
    assert ledger.pending_path.exists()

    exact = registry.acknowledge_binding_operation(account, runtime, epoch, operation)
    assert exact.nonce_count == 2
    assert not ledger.pending_path.exists()
    following = registry.acknowledge_binding_operation(account, runtime, epoch, "c" * 64)
    assert following.nonce_count == 3


@pytest.mark.parametrize(
    "crash_point",
    [
        "after_pending_before_transition",
        "after_transition_before_registry",
        "after_registry_before_cleanup",
    ],
)
def test_nonce_transition_crash_prefixes_roll_forward_exactly_once(
    tmp_path: Path,
    crash_point: str,
) -> None:
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _registry(tmp_path / f"{crash_point}.json")
    account, runtime, epoch = _binding_inputs(tmp_path / crash_point)
    registry.bind_new(account, runtime, epoch, "a" * 64)
    operation = "b" * 64
    registry._nonce_commit_fault = crash_point  # type: ignore[attr-defined]
    with pytest.raises(AccountRuntimeRegistryError, match="injected crash"):
        registry.acknowledge_binding_operation(account, runtime, epoch, operation)
    del registry._nonce_commit_fault  # type: ignore[attr-defined]

    exact = registry.acknowledge_binding_operation(account, runtime, epoch, operation)
    assert exact.nonce_count == 2
    assert registry.acknowledge_binding_operation(account, runtime, epoch, operation) == exact
    assert not _ledger(registry).pending_path.exists()


def test_forged_transition_cannot_drop_an_old_authenticated_member(tmp_path: Path) -> None:
    from afuture import account_runtime_registry as registry_module
    from afuture.account_runtime_registry import AccountRuntimeRegistryError

    registry = _registry(tmp_path / "registry.json")
    account, runtime, epoch = _binding_inputs(tmp_path)
    first_nonce = "a" * 64
    second_nonce = "b" * 64
    first = registry.bind_new(account, runtime, epoch, first_nonce)
    second = registry.acknowledge_binding_operation(account, runtime, epoch, second_nonce)
    ledger = _ledger(registry)
    second_leaf = ledger.membership_node_paths(second.nonce_root, second_nonce)[-1].stem

    current = json.loads(registry.path.read_text(encoding="utf-8"))
    current["nonce_root"] = second_leaf
    current_unsigned = {key: value for key, value in current.items() if key != "checksum"}
    current["checksum"] = registry_module._digest(current_unsigned)
    registry.path.write_text(json.dumps(current), encoding="utf-8")

    transition_path = ledger.transition_path(second.nonce_count)
    transition = json.loads(transition_path.read_text(encoding="utf-8"))
    transition["old_root"] = first.nonce_root
    transition["new_root"] = second_leaf
    transition["new_registry_checksum"] = current["checksum"]
    transition_unsigned = {key: value for key, value in transition.items() if key != "checksum"}
    transition["checksum"] = registry_module._digest(transition_unsigned)
    transition_path.write_text(
        json.dumps(
            transition,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(AccountRuntimeRegistryError, match="transition|insertion|nonce|root"):
        registry.load_required()
