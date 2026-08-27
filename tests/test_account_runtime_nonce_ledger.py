from __future__ import annotations

import json
import os
import shutil
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


def _downgrade_to_schema2_fixture(registry) -> None:
    from afuture import account_runtime_registry as registry_module

    record = registry.load_required()
    unsigned = registry_module._record_payload(
        sequence=1,
        parent_checksum=None,
        bindings=record.bindings,
        schema_version=2,
    )
    registry.path.write_text(
        json.dumps({**unsigned, "checksum": registry_module._digest(unsigned)}),
        encoding="utf-8",
    )
    registry.previous_path.unlink(missing_ok=True)
    shutil.rmtree(_ledger(registry).directory)


def test_pristine_registry_anchors_ready_empty_authenticated_nonce_dictionary(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_nonce_ledger import EMPTY_NONCE_ROOT

    registry = _registry(tmp_path / "registry.json")
    record = registry.load_required()

    assert record.nonce_root == EMPTY_NONCE_ROOT
    assert record.nonce_count == 0
    assert _ledger(registry).load_ready_root() == (EMPTY_NONCE_ROOT, 0)


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


def test_schema2_registry_requires_explicit_strong_confirmed_nonce_migration(
    tmp_path: Path,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_NONCE_LEDGER_MIGRATION_CONFIRMATION,
        AccountRuntimeRegistryError,
    )

    registry = _registry(tmp_path / "registry.json")
    account, runtime, epoch = _binding_inputs(tmp_path)
    registry.bind_new(account, runtime, epoch, "a" * 64)
    _downgrade_to_schema2_fixture(registry)

    with pytest.raises(AccountRuntimeRegistryError, match="migration"):
        registry.acknowledge_binding_operation(account, runtime, epoch, "b" * 64)
    with pytest.raises(AccountRuntimeRegistryError, match="confirmation"):
        registry.migrate_nonce_ledger(strong_confirmation="wrong")
    migrated = registry.migrate_nonce_ledger(
        strong_confirmation=ACCOUNT_RUNTIME_NONCE_LEDGER_MIGRATION_CONFIRMATION
    )
    assert migrated.nonce_count == 1
    with pytest.raises(AccountRuntimeRegistryError, match="already consumed|legacy"):
        registry.acknowledge_binding_operation(account, runtime, epoch, "a" * 64)


def test_nonce_migration_cli_is_distinct_and_strongly_confirmed() -> None:
    from afuture.cli import build_parser

    args = build_parser().parse_args(
        [
            "stress90-registry-nonce-migrate",
            "--config",
            "live.toml",
            "--confirm-live",
            "--confirm-nonce-migration",
            "--operator-reason",
            "migrate exact authoritative registry nonce history",
        ]
    )
    assert args.command == "stress90-registry-nonce-migrate"
    assert args.confirm_nonce_migration is True


@pytest.mark.parametrize(
    "crash_point",
    [
        "migration_marker_file",
        "migration_marker_parent",
        "legacy_receipt",
        "legacy_node",
        "ready_file",
        "ready_parent",
        "registry_cas",
    ],
)
def test_explicit_nonce_migration_is_exactly_retryable_at_every_durable_prefix(
    tmp_path: Path,
    crash_point: str,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_NONCE_LEDGER_MIGRATION_CONFIRMATION,
        AccountRuntimeRegistryError,
    )

    registry = _registry(tmp_path / f"{crash_point}.json")
    account, runtime, epoch = _binding_inputs(tmp_path / crash_point)
    nonce = "a" * 64
    registry.bind_new(account, runtime, epoch, nonce)
    _downgrade_to_schema2_fixture(registry)
    before = registry.require_binding_evidence(account, runtime, epoch)

    registry._nonce_migration_fault = crash_point  # type: ignore[attr-defined]
    with pytest.raises(AccountRuntimeRegistryError, match="injected|migration"):
        registry.migrate_nonce_ledger(
            strong_confirmation=ACCOUNT_RUNTIME_NONCE_LEDGER_MIGRATION_CONFIRMATION
        )
    del registry._nonce_migration_fault  # type: ignore[attr-defined]
    migrated = registry.migrate_nonce_ledger(
        strong_confirmation=ACCOUNT_RUNTIME_NONCE_LEDGER_MIGRATION_CONFIRMATION
    )
    after = registry.require_binding_evidence(account, runtime, epoch)
    assert migrated.nonce_count == 1
    assert after.binding_payload_digest == before.binding_payload_digest
    assert after.binding_revision == before.binding_revision
    assert after.binding_receipt_digest == before.binding_receipt_digest


@pytest.mark.parametrize("tamper", ["current", "current_and_previous", "missing_receipt"])
def test_migration_anchor_rejects_resigned_or_missing_legacy_history(
    tmp_path: Path,
    tamper: str,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_NONCE_LEDGER_MIGRATION_CONFIRMATION,
        AccountRuntimeRegistryError,
    )

    registry = _registry(tmp_path / f"{tamper}.json")
    account, runtime, epoch = _binding_inputs(tmp_path / tamper)
    registry.bind_new(account, runtime, epoch, "a" * 64)
    _downgrade_to_schema2_fixture(registry)
    registry._nonce_migration_fault = "legacy_receipt"  # type: ignore[attr-defined]
    with pytest.raises(AccountRuntimeRegistryError):
        registry.migrate_nonce_ledger(
            strong_confirmation=ACCOUNT_RUNTIME_NONCE_LEDGER_MIGRATION_CONFIRMATION
        )
    del registry._nonce_migration_fault  # type: ignore[attr-defined]

    if tamper == "missing_receipt":
        _ledger(registry).receipt_path("a" * 64).unlink()
        migrated = registry.migrate_nonce_ledger(
            strong_confirmation=ACCOUNT_RUNTIME_NONCE_LEDGER_MIGRATION_CONFIRMATION
        )
        assert migrated.nonce_count == 1
        _ledger(registry).receipt_path("a" * 64).unlink()
        with pytest.raises(
            AccountRuntimeRegistryError,
            match="migration|manifest|receipt|anchor|ledger",
        ):
            registry.load_required()
        return
    else:
        _resign_registry(
            registry.path,
            lambda raw: raw["bindings"][0].update(
                {"operation_history": ["b" * 64], "last_operation_id": "b" * 64}
            ),
        )  # type: ignore[index]
        if tamper == "current_and_previous" and registry.previous_path.exists():
            _resign_registry(
                registry.previous_path,
                lambda raw: raw["bindings"][0].update(
                    {"operation_history": ["b" * 64], "last_operation_id": "b" * 64}
                ),
            )  # type: ignore[index]

    with pytest.raises(AccountRuntimeRegistryError, match="migration|manifest|receipt|anchor"):
        registry.migrate_nonce_ledger(
            strong_confirmation=ACCOUNT_RUNTIME_NONCE_LEDGER_MIGRATION_CONFIRMATION
        )


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
