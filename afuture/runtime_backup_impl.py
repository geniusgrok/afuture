"""Deterministic, allowlisted Stress-90 runtime backup and fail-closed restore."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from .account_runtime_registry import AccountRuntimeRegistry
from .broker.ctp_order_journal import CtpOrderSubmissionJournal
from .deployment_identity import (
    DEPLOYMENT_IDENTITY_FILENAME,
    DeploymentIdentityStore,
    verify_deployment,
)
from .directional_activity import DirectionalActivityStore, validate_directional_activity_snapshot
from .directional_ohlc_cache import DirectionalOHLCCacheStore
from .directional_stress90_execution import Stress90ExecutionIntentStore
from .directional_stress90_oi_runtime import Stress90OiEvidenceStore
from .directional_stress90_policy import STRESS90_POLICY
from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
from .models import RuntimeMode
from .provenance import canonical_safe_path
from .runtime_lease import AccountExclusiveRuntimeLease
from .secure_archive import (
    SecureArchiveError,
    build_deterministic_archive,
    canonical_json_bytes,
    verify_archive,
)
from .state import StateStore
from .stress90_activation_permit import Stress90ActivationPermitStore
from .stress90_lifecycle_transaction import Stress90LifecycleTransactionStore
from .trading_day_evidence import TradingDayEvidenceStore

BACKUP_KIND = "afuture.runtime-backup"
BACKUP_SCHEMA_VERSION = 1
BACKUP_MAX_MEMBER_BYTES = 512 * 1024 * 1024
BACKUP_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
BACKUP_MAX_MEMBERS = 2_500_000
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SAFE_FILE = re.compile(r"[A-Za-z0-9._-]+")
_SAFE_STATE_FILE = re.compile(r"[A-Za-z0-9._-]+\.json")
_SIDECARS = ("", ".prev", ".lineage", ".lock", ".pending")
_RUNTIME_BASES = frozenset(
    {
        "ctp_trading_day_evidence.json",
        "deployment_identity.json",
        "directional_activity.json",
        "directional_ohlc_cache.json",
        "stress90_activation_permit.json",
        "stress90_bootstrap_seed.json",
        "stress90_crash_fill_recovery.json",
        "stress90_ctp_session_evidence.json",
        "stress90_execution_intent.json",
        "stress90_lifecycle_transaction.json",
        "stress90_oi_evidence.json",
        "stress90_operator_continuity.json",
        "stress90_policy_state.json",
    }
)


class RuntimeBackupError(RuntimeError):
    """Runtime backup or restore evidence cannot be trusted."""


@dataclass(frozen=True)
class BackupVerification:
    manifest: dict[str, Any]
    archive_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": True,
            "backup_digest": self.manifest["backup_digest"],
            "archive_sha256": self.archive_sha256,
            "account_identity_digest": self.manifest["account_identity_digest"],
            "account_epoch": self.manifest["account_epoch"],
            "runtime_mode": self.manifest["runtime_mode"],
            "kill_switch": self.manifest["kill_switch"],
            "orders_sent": 0,
            "cancels_sent": 0,
        }


def _sha64(value: object, label: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise RuntimeBackupError(f"{label} must be lowercase SHA-256")
    return value


def _state_filename(value: object) -> str:
    if not isinstance(value, str) or _SAFE_STATE_FILE.fullmatch(value) is None:
        raise RuntimeBackupError("backup generic state filename is invalid")
    if value in {
        "alerts.json",
        "audit.json",
        "directional_alerts.json",
        "directional_report.json",
        "report.json",
    }:
        raise RuntimeBackupError("backup generic state filename is unsafe")
    return value


def _safe_relative(name: str) -> bool:
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name:
        return False
    pure = PurePosixPath(name)
    return pure.as_posix() == name and all(part not in {"", ".", ".."} for part in pure.parts)


def _allowed_member(name: str, state_filename: str | None) -> bool:
    if name == "manifest.json":
        return True
    if not _safe_relative(name):
        return False
    parts = PurePosixPath(name).parts
    if parts[0] == "runtime":
        rel = "/".join(parts[1:])
        if not rel:
            return False
        bases = set(_RUNTIME_BASES)
        if state_filename is not None:
            bases.add(state_filename)
        if "/" not in rel and any(rel == base + suffix for base in bases for suffix in _SIDECARS):
            return True
        if "/" not in rel and rel in {
            "stress90_ctp_orders.json",
            "stress90_ctp_orders.json.epochs.json",
            "stress90_ctp_orders.json.epochs.json.prev",
            "stress90_ctp_orders.json.lock",
            "stress90_ctp_orders.json.prev",
        }:
            return True
        if "/" not in rel and re.fullmatch(
            r"stress90_ctp_orders\.json\.(?:archive\.\d{20}\.[0-9a-f]{64}|runtime-index\.[0-9a-f]{64})\.json",
            rel,
        ):
            return True
        return bool(
            len(parts) == 4
            and parts[1] == "stress90_ctp_orders.json.epochs"
            and re.fullmatch(r"epoch-[0-9a-f]{64}", parts[2])
            and _SAFE_FILE.fullmatch(parts[3])
        )
    if parts[0] == "registry":
        if len(parts) == 2 and parts[1] in {
            "account-runtime-registry.json",
            "account-runtime-registry.json.lineage",
            "account-runtime-registry.json.lock",
            "account-runtime-registry.json.prev",
        }:
            return True
        if (
            len(parts) == 3
            and parts[1] == "nonce-ledger"
            and parts[2]
            in {
                "migration.json",
                "pending.json",
                "ready.json",
            }
        ):
            return True
        return bool(
            len(parts) == 4
            and parts[1] == "nonce-ledger"
            and parts[2] in {"nodes", "receipts", "transitions"}
            and _SAFE_FILE.fullmatch(parts[3])
            and parts[3].endswith(".json")
        )
    return False


def _read_regular(path: Path) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise RuntimeBackupError(f"cannot safely read authority file: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        visible = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != visible.st_dev
            or opened.st_ino != visible.st_ino
            or opened.st_size < 0
            or opened.st_size > BACKUP_MAX_MEMBER_BYTES
        ):
            raise RuntimeBackupError(f"authority file identity is invalid: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _json_identity(payload: bytes) -> tuple[int | None, str | None]:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    sequence = raw.get("sequence")
    checksum = raw.get("checksum")
    return (
        sequence if type(sequence) is int and sequence > 0 else None,
        checksum if isinstance(checksum, str) and _HEX64.fullmatch(checksum) else None,
    )


def _artifact_meta(role: str, payload: bytes) -> dict[str, object]:
    sequence, checksum = _json_identity(payload)
    return {
        "role": role,
        "size": len(payload),
        "sha256": sha256(payload).hexdigest(),
        "sequence": sequence,
        "checksum": checksum,
    }


def _add(
    members: dict[str, bytes],
    metadata: dict[str, dict[str, object]],
    logical: str,
    physical: Path,
    role: str,
    state_filename: str,
) -> None:
    if not (physical.exists() or physical.is_symlink()):
        return
    if not _allowed_member(logical, state_filename):
        raise RuntimeBackupError(f"artifact is not allowlisted: {logical}")
    payload = _read_regular(physical)
    members[logical] = payload
    metadata[logical] = _artifact_meta(role, payload)


def _collect_runtime(
    runtime: Path, state_filename: str
) -> tuple[dict[str, bytes], dict[str, dict[str, object]]]:
    members: dict[str, bytes] = {}
    metadata: dict[str, dict[str, object]] = {}
    bases = sorted({*_RUNTIME_BASES, state_filename})
    for base in bases:
        for suffix in _SIDECARS:
            _add(
                members,
                metadata,
                f"runtime/{base}{suffix}",
                runtime / f"{base}{suffix}",
                "runtime_authority",
                state_filename,
            )

    journal = runtime / "stress90_ctp_orders.json"
    fixed = (
        journal.name,
        journal.name + ".epochs.json",
        journal.name + ".epochs.json.prev",
        journal.name + ".lock",
        journal.name + ".prev",
    )
    for name in fixed:
        _add(
            members,
            metadata,
            f"runtime/{name}",
            runtime / name,
            "ctp_order_journal",
            state_filename,
        )
    for pattern in (journal.name + ".archive.*.json", journal.name + ".runtime-index.*.json"):
        for path in sorted(runtime.glob(pattern), key=os.fspath):
            _add(
                members,
                metadata,
                f"runtime/{path.name}",
                path,
                "ctp_order_journal_archive",
                state_filename,
            )
    epoch_root = runtime / (journal.name + ".epochs")
    if epoch_root.exists() or epoch_root.is_symlink():
        if epoch_root.is_symlink() or not epoch_root.is_dir():
            raise RuntimeBackupError("CTP order epoch namespace is unsafe")
        for epoch in sorted(epoch_root.iterdir(), key=os.fspath):
            if (
                epoch.is_symlink()
                or not epoch.is_dir()
                or re.fullmatch(r"epoch-[0-9a-f]{64}", epoch.name) is None
            ):
                raise RuntimeBackupError("CTP order epoch directory is unsafe")
            for item in sorted(epoch.iterdir(), key=os.fspath):
                if (
                    item.is_symlink()
                    or not item.is_file()
                    or _SAFE_FILE.fullmatch(item.name) is None
                ):
                    raise RuntimeBackupError("CTP order epoch member is unsafe")
                _add(
                    members,
                    metadata,
                    f"runtime/{epoch_root.name}/{epoch.name}/{item.name}",
                    item,
                    "ctp_order_journal_sealed_epoch",
                    state_filename,
                )
    return members, metadata


def _collect_registry(
    registry: Path, state_filename: str
) -> tuple[dict[str, bytes], dict[str, dict[str, object]]]:
    members: dict[str, bytes] = {}
    metadata: dict[str, dict[str, object]] = {}
    for suffix in ("", ".lineage", ".lock", ".prev"):
        _add(
            members,
            metadata,
            f"registry/account-runtime-registry.json{suffix}",
            registry.with_name(registry.name + suffix),
            "account_runtime_registry",
            state_filename,
        )
    ledger = registry.with_name(registry.name + ".nonce-ledger")
    if ledger.exists() or ledger.is_symlink():
        if ledger.is_symlink() or not ledger.is_dir():
            raise RuntimeBackupError("nonce ledger root is unsafe")
        for name in ("migration.json", "pending.json", "ready.json"):
            _add(
                members,
                metadata,
                f"registry/nonce-ledger/{name}",
                ledger / name,
                "account_runtime_nonce_ledger",
                state_filename,
            )
        for directory in ("nodes", "receipts", "transitions"):
            root = ledger / directory
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                raise RuntimeBackupError("nonce ledger namespace is unsafe")
            for item in sorted(root.iterdir(), key=os.fspath):
                if (
                    item.is_symlink()
                    or not item.is_file()
                    or _SAFE_FILE.fullmatch(item.name) is None
                ):
                    raise RuntimeBackupError("nonce ledger object is unsafe")
                _add(
                    members,
                    metadata,
                    f"registry/nonce-ledger/{directory}/{item.name}",
                    item,
                    "account_runtime_nonce_ledger",
                    state_filename,
                )
    return members, metadata


def _scan_secrets(members: dict[str, bytes]) -> None:
    names = (
        "AFUTURE_CTP_ACCOUNT_ID",
        "AFUTURE_CTP_AUTH_CODE",
        "AFUTURE_CTP_INVESTOR_ID",
        "AFUTURE_CTP_INVEST_UNIT_ID",
        "AFUTURE_CTP_PASSWORD",
        "AFUTURE_CTP_USER",
    )
    needles = [value.encode() for name in names if len(value := os.getenv(name, "")) >= 8]
    for logical, payload in members.items():
        if any(needle in payload for needle in needles):
            raise RuntimeBackupError(
                f"raw credential/account text found in backup source: {logical}"
            )


def _load_optional_authority(runtime: Path) -> None:
    Stress90ExecutionIntentStore(runtime / "stress90_execution_intent.json").load_record()
    Stress90ActivationPermitStore(runtime / "stress90_activation_permit.json").load_record()
    try:
        from .broker.ctp_session_query import CtpSessionActivityEvidenceStore

        CtpSessionActivityEvidenceStore(
            runtime / "stress90_ctp_session_evidence.json"
        ).load_record()
    except FileNotFoundError:
        pass
    try:
        from .stress90_crash_fill_recovery import Stress90CrashFillRecoveryStore

        Stress90CrashFillRecoveryStore(runtime / "stress90_crash_fill_recovery.json").load_record()
    except FileNotFoundError:
        pass
    try:
        from .stress90_operator_continuity import Stress90OperatorContinuityStore

        Stress90OperatorContinuityStore(runtime / "stress90_operator_continuity.json").load_record()
    except FileNotFoundError:
        pass


def _authority(
    *,
    runtime: Path,
    state_path: Path,
    registry_path: Path,
    bound_runtime: Path,
    bound_registry_path: Path,
    config=None,
    config_path: str | Path | None = None,
) -> dict[str, object]:
    generic = StateStore(state_path).load_required_record()
    state = generic.state
    if state.runtime_mode != RuntimeMode.HALTED.value or state.kill_switch is not True:
        raise RuntimeBackupError("backup requires HALTED generic state and kill switch=true")

    seed = Stress90SeedStore(runtime / "stress90_bootstrap_seed.json").load_required()
    policy_record = Stress90PolicyStateStore(
        runtime / "stress90_policy_state.json"
    ).load_required_record()
    policy = policy_record.state
    if policy.bootstrap_seed_digest != seed.seed_digest:
        raise RuntimeBackupError("seed/policy identity mismatch")
    account = policy.live_account_identity_digest
    epoch = policy.live_account_epoch
    if account is None or epoch is None:
        raise RuntimeBackupError("backup requires one bound Stress-90 account")

    lifecycle = Stress90LifecycleTransactionStore(
        runtime / "stress90_lifecycle_transaction.json"
    ).load()
    if lifecycle is not None and lifecycle.status == "prepared":
        raise RuntimeBackupError("prepared lifecycle transaction blocks backup")

    oi = Stress90OiEvidenceStore(runtime / "stress90_oi_evidence.json").load_required_record()
    tde = TradingDayEvidenceStore(runtime / "ctp_trading_day_evidence.json").load_required()
    activity = DirectionalActivityStore(runtime / "directional_activity.json").load()
    if activity is None:
        raise RuntimeBackupError("directional activity is missing")
    validate_directional_activity_snapshot(activity)
    if (
        DirectionalOHLCCacheStore(runtime / "directional_ohlc_cache.json").load(
            STRESS90_POLICY.products
        )
        is None
    ):
        raise RuntimeBackupError("directional OHLC cache is missing")

    registry = AccountRuntimeRegistry(registry_path)
    binding = registry.require_binding_evidence(account, bound_runtime, epoch)
    if str(bound_registry_path) != str(bound_registry_path.resolve(strict=False)):
        raise RuntimeBackupError("bound registry path is not canonical")
    if (
        tde.account_identity_digest != account
        or tde.account_epoch != epoch
        or tde.canonical_runtime != str(bound_runtime)
        or tde.runtime_identity_digest != binding.binding.runtime_identity_digest
        or tde.account_binding_receipt_digest != binding.binding_receipt_digest
    ):
        raise RuntimeBackupError("TradingDayEvidence/account registry identity mismatch")

    CtpOrderSubmissionJournal(runtime / "stress90_ctp_orders.json").audit_epochs()
    _load_optional_authority(runtime)
    deployment = DeploymentIdentityStore(runtime / DEPLOYMENT_IDENTITY_FILENAME).load_required()
    identity = deployment.identity
    if (
        identity.get("runtime_path") != str(bound_runtime)
        or identity.get("account_registry_path") != str(bound_registry_path)
        or identity.get("seed_digest") != seed.seed_digest
        or identity.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
        or identity.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
    ):
        raise RuntimeBackupError("deployment/runtime/bundle identity mismatch")
    if config is not None and config_path is not None:
        current = verify_deployment(
            config=config,
            config_path=config_path,
            runtime_dir=bound_runtime,
            account_registry_path=bound_registry_path,
        )
        if not current.passed:
            raise RuntimeBackupError(
                "deployment verification failed: " + ", ".join(current.changes)
            )

    return {
        "generic_sequence": generic.sequence,
        "generic_checksum": generic.checksum,
        "policy_sequence": policy_record.sequence,
        "policy_checksum": policy_record.checksum,
        "oi_sequence": oi.sequence,
        "oi_checksum": oi.checksum,
        "account_identity_digest": account,
        "account_epoch": epoch,
        "registry_sequence": binding.registry_sequence,
        "registry_checksum": binding.registry_checksum,
        "registry_binding_receipt_digest": binding.binding_receipt_digest,
        "seed_digest": seed.seed_digest,
        "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
        "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
        "deployment_sequence": deployment.sequence,
        "deployment_checksum": deployment.checksum,
        "bundle_digest": identity.get("bundle_digest"),
        "runtime_mode": state.runtime_mode,
        "kill_switch": state.kill_switch,
    }


def _backup_digest(unsigned: dict[str, Any]) -> str:
    return sha256(canonical_json_bytes(unsigned)).hexdigest()


def backup_runtime(
    *,
    config,
    config_path: str | Path,
    output_path: str | Path,
) -> BackupVerification:
    """Create one deterministic backup from a verified HALTED single-account runtime."""

    runtime = canonical_safe_path(Path(config.state_path).parent, label="runtime")
    state_path = canonical_safe_path(config.state_path, label="generic state")
    if state_path.parent != runtime:
        raise RuntimeBackupError("generic state must be inside canonical runtime")
    state_filename = _state_filename(state_path.name)
    registry_path = canonical_safe_path(config.account_registry_path, label="account registry")
    policy = Stress90PolicyStateStore(runtime / "stress90_policy_state.json").load_required()
    account = policy.live_account_identity_digest
    if account is None:
        raise RuntimeBackupError("backup requires a bound account identity")

    lease = AccountExclusiveRuntimeLease(runtime, account, role="backup-runtime")
    lease.acquire()
    try:
        before = _authority(
            runtime=runtime,
            state_path=state_path,
            registry_path=registry_path,
            bound_runtime=runtime,
            bound_registry_path=registry_path,
            config=config,
            config_path=config_path,
        )
        if not lease.authorizes_technical_activation(account, runtime):
            raise RuntimeBackupError("exact account/runtime lease is not held")
        runtime_members, runtime_meta = _collect_runtime(runtime, state_filename)
        registry_members, registry_meta = _collect_registry(registry_path, state_filename)
        members = {**runtime_members, **registry_members}
        metadata = {**runtime_meta, **registry_meta}
        _scan_secrets(members)
        after = _authority(
            runtime=runtime,
            state_path=state_path,
            registry_path=registry_path,
            bound_runtime=runtime,
            bound_registry_path=registry_path,
            config=config,
            config_path=config_path,
        )
        if before != after:
            raise RuntimeBackupError("authoritative state changed during backup")
        unsigned: dict[str, Any] = {
            "kind": BACKUP_KIND,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "source_runtime_path": str(runtime),
            "source_account_registry_path": str(registry_path),
            "state_filename": state_filename,
            **before,
            "artifacts": {name: metadata[name] for name in sorted(metadata)},
        }
        manifest = {**unsigned, "backup_digest": _backup_digest(unsigned)}
        archive_members = {"manifest.json": canonical_json_bytes(manifest), **members}
        build_deterministic_archive(
            output_path,
            archive_members,
            max_member_bytes=BACKUP_MAX_MEMBER_BYTES,
            max_total_bytes=BACKUP_MAX_TOTAL_BYTES,
            max_members=BACKUP_MAX_MEMBERS,
        )
        return verify_backup_archive(output_path)
    except RuntimeBackupError:
        raise
    except (OSError, RuntimeError, SecureArchiveError, ValueError) as exc:
        raise RuntimeBackupError(str(exc)) from exc
    finally:
        lease.release()


def _decode_manifest(payload: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeBackupError(f"duplicate backup manifest key: {key}")
            result[key] = value
        return result

    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except RuntimeBackupError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeBackupError("backup manifest is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise RuntimeBackupError("backup manifest must be an object")
    return raw


def _validate_manifest(manifest: dict[str, Any], members: dict[str, bytes]) -> str:
    fields = {
        "account_epoch",
        "account_identity_digest",
        "artifacts",
        "backup_digest",
        "bundle_digest",
        "deployment_checksum",
        "deployment_sequence",
        "generic_checksum",
        "generic_sequence",
        "kill_switch",
        "kind",
        "oi_checksum",
        "oi_sequence",
        "policy_checksum",
        "policy_definition_digest",
        "policy_sequence",
        "products_manifest_digest",
        "registry_binding_receipt_digest",
        "registry_checksum",
        "registry_sequence",
        "schema_version",
        "seed_digest",
        "source_account_registry_path",
        "source_runtime_path",
        "state_filename",
        "runtime_mode",
    }
    if set(manifest) != fields or manifest.get("kind") != BACKUP_KIND:
        raise RuntimeBackupError("backup manifest fields or kind are invalid")
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise RuntimeBackupError("backup schema is unsupported")
    state_filename = _state_filename(manifest.get("state_filename"))
    if (
        manifest.get("runtime_mode") != RuntimeMode.HALTED.value
        or manifest.get("kill_switch") is not True
    ):
        raise RuntimeBackupError("backup source was not HALTED with kill switch=true")
    if manifest.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest:
        raise RuntimeBackupError("backup policy definition mismatch")
    if manifest.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest:
        raise RuntimeBackupError("backup products manifest mismatch")
    for key in (
        "account_epoch",
        "account_identity_digest",
        "backup_digest",
        "bundle_digest",
        "deployment_checksum",
        "generic_checksum",
        "oi_checksum",
        "policy_checksum",
        "registry_binding_receipt_digest",
        "registry_checksum",
        "seed_digest",
    ):
        _sha64(manifest.get(key), key)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise RuntimeBackupError("backup artifact manifest is empty")
    if set(artifacts) != set(members) - {"manifest.json"}:
        raise RuntimeBackupError("backup artifact/member set mismatch")
    for logical, meta in artifacts.items():
        if not _allowed_member(logical, state_filename):
            raise RuntimeBackupError(f"unknown backup artifact: {logical}")
        if not isinstance(meta, dict) or set(meta) != {
            "checksum",
            "role",
            "sequence",
            "sha256",
            "size",
        }:
            raise RuntimeBackupError(f"backup artifact metadata is invalid: {logical}")
        payload = members[logical]
        if type(meta["size"]) is not int or meta["size"] != len(payload):
            raise RuntimeBackupError(f"backup artifact size mismatch: {logical}")
        if _sha64(meta["sha256"], f"{logical} sha256") != sha256(payload).hexdigest():
            raise RuntimeBackupError(f"backup artifact digest mismatch: {logical}")
    unsigned = {key: value for key, value in manifest.items() if key != "backup_digest"}
    if manifest["backup_digest"] != _backup_digest(unsigned):
        raise RuntimeBackupError("backup total digest mismatch")
    if members["manifest.json"] != canonical_json_bytes(manifest):
        raise RuntimeBackupError("backup manifest is not canonical JSON")
    return state_filename


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("durable file write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _materialize(
    *,
    root: Path,
    members: dict[str, bytes],
    registry_filename: str,
) -> tuple[Path, Path]:
    runtime = root / "runtime"
    registry_root = root / "registry"
    runtime.mkdir()
    registry_root.mkdir()
    registry = registry_root / registry_filename
    for logical, payload in members.items():
        if logical == "manifest.json":
            continue
        parts = PurePosixPath(logical).parts
        if parts[0] == "runtime":
            target = runtime.joinpath(*parts[1:])
        elif parts[:2] == ("registry", "nonce-ledger"):
            target = registry_root / (registry_filename + ".nonce-ledger")
            target = target.joinpath(*parts[2:])
        elif parts[0] == "registry":
            suffix = parts[1].removeprefix("account-runtime-registry.json")
            target = registry.with_name(registry.name + suffix)
        else:
            raise RuntimeBackupError("backup artifact root is invalid")
        _write_exact(target, payload)
    return runtime, registry


def _semantic_verify(
    *,
    runtime: Path,
    state_path: Path,
    registry_path: Path,
    manifest: dict[str, Any],
) -> dict[str, object]:
    return _authority(
        runtime=runtime,
        state_path=state_path,
        registry_path=registry_path,
        bound_runtime=Path(str(manifest["source_runtime_path"])),
        bound_registry_path=Path(str(manifest["source_account_registry_path"])),
    )


def verify_backup_archive(path: str | Path) -> BackupVerification:
    """Read-only verification of archive safety and cross-file authority identity."""

    try:
        archive = verify_archive(
            path,
            max_member_bytes=BACKUP_MAX_MEMBER_BYTES,
            max_total_bytes=BACKUP_MAX_TOTAL_BYTES,
            max_members=BACKUP_MAX_MEMBERS,
        )
        members = dict(archive.members)
        if "manifest.json" not in members:
            raise RuntimeBackupError("backup manifest is missing")
        manifest = _decode_manifest(members["manifest.json"])
        state_filename = _validate_manifest(manifest, members)
        if any(not _allowed_member(name, state_filename) for name in members):
            raise RuntimeBackupError("backup contains an unknown artifact")
        source_registry = Path(str(manifest["source_account_registry_path"]))
        registry_filename = source_registry.name
        if registry_filename != "account-runtime-registry.json":
            raise RuntimeBackupError("backup registry filename is unsupported")
        with tempfile.TemporaryDirectory(prefix="afuture-backup-verify-") as temp_name:
            runtime, registry = _materialize(
                root=Path(temp_name),
                members=members,
                registry_filename=registry_filename,
            )
            observed = _semantic_verify(
                runtime=runtime,
                state_path=runtime / state_filename,
                registry_path=registry,
                manifest=manifest,
            )
            keys = (
                "account_epoch",
                "account_identity_digest",
                "bundle_digest",
                "deployment_checksum",
                "deployment_sequence",
                "generic_checksum",
                "generic_sequence",
                "kill_switch",
                "oi_checksum",
                "oi_sequence",
                "policy_checksum",
                "policy_definition_digest",
                "policy_sequence",
                "products_manifest_digest",
                "registry_binding_receipt_digest",
                "registry_checksum",
                "registry_sequence",
                "runtime_mode",
                "seed_digest",
            )
            if {key: observed[key] for key in keys} != {key: manifest[key] for key in keys}:
                raise RuntimeBackupError("backup cross-file authority identity mismatch")
        return BackupVerification(manifest=manifest, archive_sha256=archive.archive_sha256)
    except RuntimeBackupError:
        raise
    except (OSError, RuntimeError, SecureArchiveError, ValueError) as exc:
        raise RuntimeBackupError(str(exc)) from exc


def _require_empty_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise RuntimeBackupError(f"{label} symlink is forbidden")
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise RuntimeBackupError(f"{label} must be empty")


def _require_registry_absent(path: Path) -> None:
    paths = (
        path,
        path.with_name(path.name + ".lineage"),
        path.with_name(path.name + ".lock"),
        path.with_name(path.name + ".nonce-ledger"),
        path.with_name(path.name + ".prev"),
    )
    if any(item.exists() or item.is_symlink() for item in paths):
        raise RuntimeBackupError("restore refuses existing account registry authority")


def _publish_registry(stage_registry: Path, final_registry: Path) -> None:
    staged_ledger = stage_registry.with_name(stage_registry.name + ".nonce-ledger")
    final_ledger = final_registry.with_name(final_registry.name + ".nonce-ledger")
    if staged_ledger.exists():
        os.rename(staged_ledger, final_ledger)
        _fsync_dir(final_registry.parent)
    for suffix in (".lineage", ".prev", ".lock"):
        source = stage_registry.with_name(stage_registry.name + suffix)
        if source.exists():
            os.rename(source, final_registry.with_name(final_registry.name + suffix))
            _fsync_dir(final_registry.parent)
    os.rename(stage_registry, final_registry)
    _fsync_dir(final_registry.parent)


def restore_runtime(
    *,
    backup_path: str | Path,
    runtime_dir: str | Path,
    account_registry_path: str | Path,
    registry_staging_path: str | Path,
) -> dict[str, object]:
    """Restore verified authority only to empty canonical paths and remain HALTED."""

    verification = verify_backup_archive(backup_path)
    manifest = verification.manifest
    state_filename = _state_filename(manifest["state_filename"])
    runtime = canonical_safe_path(runtime_dir, label="restore runtime")
    registry = canonical_safe_path(account_registry_path, label="restore account registry")
    registry_stage_root = canonical_safe_path(
        registry_staging_path,
        label="registry restore staging",
    )
    if str(runtime) != manifest["source_runtime_path"]:
        raise RuntimeBackupError("restore runtime differs from backed canonical runtime")
    if str(registry) != manifest["source_account_registry_path"]:
        raise RuntimeBackupError("restore registry differs from backed canonical registry")
    if registry_stage_root == registry.parent:
        raise RuntimeBackupError("registry staging path must be a distinct directory")
    _require_empty_directory(runtime, "restore runtime")
    _require_empty_directory(registry_stage_root, "registry staging path")
    _require_registry_absent(registry)

    archive = verify_archive(
        backup_path,
        max_member_bytes=BACKUP_MAX_MEMBER_BYTES,
        max_total_bytes=BACKUP_MAX_TOTAL_BYTES,
        max_members=BACKUP_MAX_MEMBERS,
    )
    members = dict(archive.members)
    runtime.parent.mkdir(parents=True, exist_ok=True)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry_stage_root.parent.mkdir(parents=True, exist_ok=True)
    if registry_stage_root.exists():
        registry_stage_root.rmdir()
    registry_stage_root.mkdir(mode=0o700)
    runtime_stage = Path(tempfile.mkdtemp(prefix=f".{runtime.name}.restore-", dir=runtime.parent))
    stage_registry = registry_stage_root / registry.name
    runtime_published = False
    try:
        for logical, payload in members.items():
            if logical == "manifest.json":
                continue
            parts = PurePosixPath(logical).parts
            if parts[0] == "runtime":
                target = runtime_stage.joinpath(*parts[1:])
            elif parts[:2] == ("registry", "nonce-ledger"):
                target = registry_stage_root / (registry.name + ".nonce-ledger")
                target = target.joinpath(*parts[2:])
            elif parts[0] == "registry":
                suffix = parts[1].removeprefix("account-runtime-registry.json")
                target = stage_registry.with_name(stage_registry.name + suffix)
            else:
                raise RuntimeBackupError("restore artifact root is invalid")
            _write_exact(target, payload)

        # Validate staged bytes against their original canonical bindings before publication.
        observed = _semantic_verify(
            runtime=runtime_stage,
            state_path=runtime_stage / state_filename,
            registry_path=stage_registry,
            manifest=manifest,
        )
        if (
            observed["runtime_mode"] != RuntimeMode.HALTED.value
            or observed["kill_switch"] is not True
        ):
            raise RuntimeBackupError("restored generic source is not HALTED and kill-switched")
        permit_store = Stress90ActivationPermitStore(
            runtime_stage / "stress90_activation_permit.json"
        )
        permit = permit_store.load_record()
        if permit is not None and permit.permit.status != "invalidated":
            permit_store.invalidate(
                "runtime restored from verified backup; fresh Doctor permit required"
            )
        generic = StateStore(runtime_stage / state_filename).load_required_record()
        if (
            generic.state.runtime_mode != RuntimeMode.HALTED.value
            or generic.state.kill_switch is not True
        ):
            raise RuntimeBackupError("restore did not preserve HALTED kill-switch state")
        _fsync_dir(runtime_stage)
        _fsync_dir(registry_stage_root)

        _publish_registry(stage_registry, registry)
        if runtime.exists():
            if any(runtime.iterdir()):
                raise RuntimeBackupError("runtime became non-empty before atomic publish")
            runtime.rmdir()
            _fsync_dir(runtime.parent)
        os.rename(runtime_stage, runtime)
        runtime_published = True
        _fsync_dir(runtime.parent)
        registry_stage_root.rmdir()
        _fsync_dir(registry_stage_root.parent)

        final_state = StateStore(runtime / state_filename).load_required_record().state
        final_policy = Stress90PolicyStateStore(
            runtime / "stress90_policy_state.json"
        ).load_required()
        final_registry = AccountRuntimeRegistry(registry).require_binding_evidence(
            final_policy.live_account_identity_digest or "",
            runtime,
            final_policy.live_account_epoch or "",
        )
        del final_registry
        final_permit = Stress90ActivationPermitStore(
            runtime / "stress90_activation_permit.json"
        ).load_record()
        if (
            final_state.runtime_mode != RuntimeMode.HALTED.value
            or final_state.kill_switch is not True
        ):
            raise RuntimeBackupError("restored runtime is not HALTED with kill switch=true")
        if final_permit is not None and final_permit.permit.status == "issued":
            raise RuntimeBackupError("restored runtime retained an issued activation permit")
        return {
            "restored": True,
            "runtime_dir": str(runtime),
            "account_registry_path": str(registry),
            "runtime_mode": RuntimeMode.HALTED.value,
            "kill_switch": True,
            "activation_permit_valid": False,
            "orders_sent": 0,
            "cancels_sent": 0,
            "next_steps": ["status", "deployment-verify", "doctor", "issue-fresh-permit"],
        }
    except Exception:
        if not runtime_published and runtime_stage.exists():
            shutil.rmtree(runtime_stage, ignore_errors=True)
        raise
    finally:
        if registry_stage_root.exists() and not any(registry_stage_root.iterdir()):
            try:
                registry_stage_root.rmdir()
            except OSError:
                pass
