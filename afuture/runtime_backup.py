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
BACKUP_MAX_MEMBERS = 2_000_000
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9._-]+")

_RUNTIME_BASES = frozenset(
    {
        "state.json",
        "stress90_policy_state.json",
        "stress90_bootstrap_seed.json",
        "stress90_oi_evidence.json",
        "ctp_trading_day_evidence.json",
        "stress90_lifecycle_transaction.json",
        "stress90_activation_permit.json",
        "stress90_operator_continuity.json",
        "stress90_crash_fill_recovery.json",
        "stress90_ctp_session_evidence.json",
        "directional_ohlc_cache.json",
        "directional_activity.json",
        "stress90_execution_intent.json",
        DEPLOYMENT_IDENTITY_FILENAME,
    }
)
_SIDECAR_SUFFIXES = ("", ".prev", ".lineage", ".lock", ".pending")


class RuntimeBackupError(RuntimeError):
    """Runtime backup/restore evidence cannot be trusted."""


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


def _is_safe_relative(name: str) -> bool:
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name:
        return False
    pure = PurePosixPath(name)
    return pure.as_posix() == name and all(part not in {"", ".", ".."} for part in pure.parts)


def _allowed_logical_member(name: str) -> bool:
    if name == "manifest.json":
        return True
    if not _is_safe_relative(name):
        return False
    parts = PurePosixPath(name).parts
    if parts[0] == "runtime":
        rel = "/".join(parts[1:])
        if not rel:
            return False
        if "/" not in rel:
            if any(rel == base + suffix for base in _RUNTIME_BASES for suffix in _SIDECAR_SUFFIXES):
                return True
            if rel in {
                "stress90_ctp_orders.json",
                "stress90_ctp_orders.json.prev",
                "stress90_ctp_orders.json.lock",
                "stress90_ctp_orders.json.epochs.json",
                "stress90_ctp_orders.json.epochs.json.prev",
            }:
                return True
            if re.fullmatch(
                r"stress90_ctp_orders\.json\.(?:archive\.\d{20}\.[0-9a-f]{64}|runtime-index\.[0-9a-f]{64})\.json",
                rel,
            ):
                return True
            return False
        if parts[1] == "stress90_ctp_orders.json.epochs" and len(parts) == 4:
            return bool(
                re.fullmatch(r"epoch-[0-9a-f]{64}", parts[2])
                and _SAFE_FILENAME.fullmatch(parts[3])
            )
        return False
    if parts[0] == "registry":
        if len(parts) == 2 and parts[1] in {
            "account-runtime-registry.json",
            "account-runtime-registry.json.prev",
            "account-runtime-registry.json.lineage",
            "account-runtime-registry.json.lock",
        }:
            return True
        if len(parts) == 3 and parts[1] == "nonce-ledger" and parts[2] in {
            "ready.json",
            "pending.json",
            "migration.json",
        }:
            return True
        if (
            len(parts) == 4
            and parts[1] == "nonce-ledger"
            and parts[2] in {"receipts", "nodes", "transitions"}
            and _SAFE_FILENAME.fullmatch(parts[3])
            and parts[3].endswith(".json")
        ):
            return True
        return False
    return False


def _sha64(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise RuntimeBackupError(f"{label} must be lowercase SHA-256")
    return value


def _json_meta(payload: bytes) -> tuple[int | None, str | None]:
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


def _member_meta(role: str, payload: bytes) -> dict[str, object]:
    sequence, checksum = _json_meta(payload)
    return {
        "role": role,
        "size": len(payload),
        "sha256": sha256(payload).hexdigest(),
        "sequence": sequence,
        "checksum": checksum,
    }


def _read_regular(path: Path) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise RuntimeBackupError(f"authoritative artifact cannot be opened safely: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        visible = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != visible.st_dev
            or metadata.st_ino != visible.st_ino
            or metadata.st_size > BACKUP_MAX_MEMBER_BYTES
        ):
            raise RuntimeBackupError(f"authoritative artifact identity is invalid: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _add_existing(
    members: dict[str, bytes],
    metadata: dict[str, dict[str, object]],
    *,
    logical: str,
    physical: Path,
    role: str,
) -> None:
    exists = physical.exists() or physical.is_symlink()
    if not exists:
        return
    if not _allowed_logical_member(logical):
        raise RuntimeBackupError(f"backup logical path is not allowlisted: {logical}")
    payload = _read_regular(physical)
    members[logical] = payload
    metadata[logical] = _member_meta(role, payload)


def _runtime_members(runtime: Path, state_name: str) -> tuple[dict[str, bytes], dict[str, dict[str, object]]]:
    members: dict[str, bytes] = {}
    metadata: dict[str, dict[str, object]] = {}
    dynamic_bases = set(_RUNTIME_BASES)
    dynamic_bases.discard("state.json")
    dynamic_bases.add(state_name)
    for base in sorted(dynamic_bases):
        for suffix in _SIDECAR_SUFFIXES:
            path = runtime / (base + suffix)
            _add_existing(
                members,
                metadata,
                logical=f"runtime/{base}{suffix}",
                physical=path,
                role="runtime_authority",
            )

    journal = runtime / "stress90_ctp_orders.json"
    for name in (
        journal.name,
        journal.name + ".prev",
        journal.name + ".lock",
        journal.name + ".epochs.json",
        journal.name + ".epochs.json.prev",
    ):
        _add_existing(
            members,
            metadata,
            logical=f"runtime/{name}",
            physical=runtime / name,
            role="ctp_order_journal",
        )
    for pattern in (
        journal.name + ".archive.*.json",
        journal.name + ".runtime-index.*.json",
    ):
        for path in sorted(runtime.glob(pattern), key=os.fspath):
            _add_existing(
                members,
                metadata,
                logical=f"runtime/{path.name}",
                physical=path,
                role="ctp_order_journal_archive",
            )
    epoch_root = runtime / (journal.name + ".epochs")
    if epoch_root.exists() or epoch_root.is_symlink():
        if epoch_root.is_symlink() or not epoch_root.is_dir():
            raise RuntimeBackupError("CTP order journal epoch root is unsafe")
        for epoch in sorted(epoch_root.iterdir(), key=os.fspath):
            if epoch.is_symlink() or not epoch.is_dir() or re.fullmatch(r"epoch-[0-9a-f]{64}", epoch.name) is None:
                raise RuntimeBackupError("CTP order journal epoch directory is unsafe")
            for item in sorted(epoch.iterdir(), key=os.fspath):
                if item.is_symlink() or not item.is_file() or _SAFE_FILENAME.fullmatch(item.name) is None:
                    raise RuntimeBackupError("CTP order journal epoch artifact is unsafe")
                _add_existing(
                    members,
                    metadata,
                    logical=f"runtime/{epoch_root.name}/{epoch.name}/{item.name}",
                    physical=item,
                    role="ctp_order_journal_sealed_epoch",
                )
    return members, metadata


def _registry_members(registry_path: Path) -> tuple[dict[str, bytes], dict[str, dict[str, object]]]:
    members: dict[str, bytes] = {}
    metadata: dict[str, dict[str, object]] = {}
    for suffix in ("", ".prev", ".lineage", ".lock"):
        _add_existing(
            members,
            metadata,
            logical=f"registry/account-runtime-registry.json{suffix}",
            physical=registry_path.with_name(registry_path.name + suffix),
            role="account_runtime_registry",
        )
    ledger = registry_path.with_name(registry_path.name + ".nonce-ledger")
    if ledger.exists() or ledger.is_symlink():
        if ledger.is_symlink() or not ledger.is_dir():
            raise RuntimeBackupError("account runtime nonce ledger root is unsafe")
        for filename in ("ready.json", "pending.json", "migration.json"):
            _add_existing(
                members,
                metadata,
                logical=f"registry/nonce-ledger/{filename}",
                physical=ledger / filename,
                role="account_runtime_nonce_ledger",
            )
        for directory in ("receipts", "nodes", "transitions"):
            root = ledger / directory
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                raise RuntimeBackupError("account runtime nonce ledger namespace is unsafe")
            for item in sorted(root.iterdir(), key=os.fspath):
                if item.is_symlink() or not item.is_file() or _SAFE_FILENAME.fullmatch(item.name) is None:
                    raise RuntimeBackupError("account runtime nonce ledger object is unsafe")
                _add_existing(
                    members,
                    metadata,
                    logical=f"registry/nonce-ledger/{directory}/{item.name}",
                    physical=item,
                    role="account_runtime_nonce_ledger",
                )
    return members, metadata


def _scan_for_secrets(members: dict[str, bytes]) -> None:
    names = (
        "AFUTURE_CTP_PASSWORD",
        "AFUTURE_CTP_AUTH_CODE",
        "AFUTURE_CTP_USER",
        "AFUTURE_CTP_ACCOUNT_ID",
        "AFUTURE_CTP_INVESTOR_ID",
        "AFUTURE_CTP_INVEST_UNIT_ID",
    )
    secrets = [os.getenv(name, "") for name in names]
    needles = [value.encode("utf-8") for value in secrets if len(value) >= 4]
    for logical, payload in members.items():
        if any(needle in payload for needle in needles):
            raise RuntimeBackupError(f"authoritative artifact contains forbidden raw credential/account text: {logical}")


def _runtime_identity_digest(runtime: Path) -> str:
    return sha256(f"runtime:{runtime}".encode()).hexdigest()


def _validate_runtime_authority(
    *,
    runtime: Path,
    state_path: Path,
    registry_path: Path,
    config=None,
    config_path: str | Path | None = None,
) -> dict[str, object]:
    generic_record = StateStore(state_path).load_required_record()
    state = generic_record.state
    if state.runtime_mode != RuntimeMode.HALTED.value or state.kill_switch is not True:
        raise RuntimeBackupError("runtime backup requires generic HALTED state and kill switch=true")
    seed = Stress90SeedStore(runtime / "stress90_bootstrap_seed.json").load_required()
    policy_record = Stress90PolicyStateStore(runtime / "stress90_policy_state.json").load_required_record()
    policy = policy_record.state
    if policy.bootstrap_seed_digest != seed.seed_digest:
        raise RuntimeBackupError("runtime backup seed/policy identity mismatch")
    if policy.live_account_identity_digest is None or policy.live_account_epoch is None:
        raise RuntimeBackupError("runtime backup requires one bound supported Stress-90 account")
    lifecycle = Stress90LifecycleTransactionStore(
        runtime / "stress90_lifecycle_transaction.json"
    ).load()
    if lifecycle is not None and lifecycle.status == "prepared":
        raise RuntimeBackupError("runtime backup is blocked by prepared lifecycle transaction")
    oi = Stress90OiEvidenceStore(runtime / "stress90_oi_evidence.json").load_required_record()
    trading_day = TradingDayEvidenceStore(runtime / "ctp_trading_day_evidence.json").load_required()
    activity = DirectionalActivityStore(runtime / "directional_activity.json").load()
    if activity is None:
        raise RuntimeBackupError("runtime backup directional activity is missing")
    validate_directional_activity_snapshot(activity)
    ohlc = DirectionalOHLCCacheStore(runtime / "directional_ohlc_cache.json").load(
        STRESS90_POLICY.products
    )
    if ohlc is None:
        raise RuntimeBackupError("runtime backup OHLC cache is missing")
    registry = AccountRuntimeRegistry(registry_path)
    binding = registry.require_binding_evidence(
        policy.live_account_identity_digest,
        runtime,
        policy.live_account_epoch,
    )
    CtpOrderSubmissionJournal(runtime / "stress90_ctp_orders.json").audit_epochs()
    Stress90ExecutionIntentStore(runtime / "stress90_execution_intent.json").load_record()
    Stress90ActivationPermitStore(runtime / "stress90_activation_permit.json").load_record()
    deployment = DeploymentIdentityStore(runtime / DEPLOYMENT_IDENTITY_FILENAME).load_required()
    if config is not None and config_path is not None:
        deployment_verification = verify_deployment(
            config=config,
            config_path=config_path,
            runtime_dir=runtime,
            account_registry_path=registry_path,
        )
        if not deployment_verification.passed:
            raise RuntimeBackupError(
                "runtime backup deployment verification failed: "
                + ", ".join(deployment_verification.changes)
            )
    if (
        deployment.identity.get("runtime_path") != str(runtime)
        or deployment.identity.get("account_registry_path") != str(registry_path)
        or deployment.identity.get("seed_digest") != seed.seed_digest
        or deployment.identity.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
        or deployment.identity.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
    ):
        raise RuntimeBackupError("runtime backup deployment/bundle identity mismatch")
    if trading_day.account_identity_digest != policy.live_account_identity_digest:
        raise RuntimeBackupError("runtime backup TradingDayEvidence account identity mismatch")
    return {
        "generic_sequence": generic_record.sequence,
        "generic_checksum": generic_record.checksum,
        "policy_sequence": policy_record.sequence,
        "policy_checksum": policy_record.checksum,
        "oi_sequence": oi.sequence,
        "oi_checksum": oi.checksum,
        "account_identity_digest": policy.live_account_identity_digest,
        "account_epoch": policy.live_account_epoch,
        "registry_sequence": binding.registry_sequence,
        "registry_checksum": binding.registry_checksum,
        "registry_binding_receipt_digest": binding.binding_receipt_digest,
        "seed_digest": seed.seed_digest,
        "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
        "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
        "deployment_sequence": deployment.sequence,
        "deployment_checksum": deployment.checksum,
        "bundle_digest": deployment.identity.get("bundle_digest"),
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
    """Backup one HALTED Stress-90 runtime under the account/runtime lease."""

    runtime = canonical_safe_path(Path(config.state_path).parent, label="runtime")
    state_path = canonical_safe_path(config.state_path, label="generic state")
    registry_path = canonical_safe_path(config.account_registry_path, label="account registry")
    pre_policy = Stress90PolicyStateStore(runtime / "stress90_policy_state.json").load_required()
    account = pre_policy.live_account_identity_digest
    if account is None:
        raise RuntimeBackupError("runtime backup requires a bound account identity")
    lease = AccountExclusiveRuntimeLease(runtime, account, role="backup-runtime")
    lease.acquire()
    try:
        authority = _validate_runtime_authority(
            runtime=runtime,
            state_path=state_path,
            registry_path=registry_path,
            config=config,
            config_path=config_path,
        )
        if not lease.authorizes_technical_activation(account, runtime):
            raise RuntimeBackupError("runtime backup lost the exact account/runtime lease")
        runtime_members, runtime_meta = _runtime_members(runtime, state_path.name)
        registry_members, registry_meta = _registry_members(registry_path)
        members = {**runtime_members, **registry_members}
        metadata = {**runtime_meta, **registry_meta}
        if not members:
            raise RuntimeBackupError("runtime backup contains no authoritative artifacts")
        _scan_for_secrets(members)
        # Revalidate after byte capture while the lease is still held.
        authority_after = _validate_runtime_authority(
            runtime=runtime,
            state_path=state_path,
            registry_path=registry_path,
            config=config,
            config_path=config_path,
        )
        if authority_after != authority:
            raise RuntimeBackupError("runtime authority changed during backup capture")
        unsigned: dict[str, Any] = {
            "kind": BACKUP_KIND,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "source_runtime_path": str(runtime),
            "source_account_registry_path": str(registry_path),
            "runtime_identity_digest": _runtime_identity_digest(runtime),
            **authority,
            "artifacts": {key: metadata[key] for key in sorted(metadata)},
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
    except (OSError, RuntimeError, ValueError, SecureArchiveError) as exc:
        raise RuntimeBackupError(str(exc)) from exc
    finally:
        lease.release()


def _validate_manifest(manifest: dict[str, Any], members: dict[str, bytes]) -> None:
    required = {
        "kind",
        "schema_version",
        "source_runtime_path",
        "source_account_registry_path",
        "runtime_identity_digest",
        "generic_sequence",
        "generic_checksum",
        "policy_sequence",
        "policy_checksum",
        "oi_sequence",
        "oi_checksum",
        "account_identity_digest",
        "account_epoch",
        "registry_sequence",
        "registry_checksum",
        "registry_binding_receipt_digest",
        "seed_digest",
        "policy_definition_digest",
        "products_manifest_digest",
        "deployment_sequence",
        "deployment_checksum",
        "bundle_digest",
        "runtime_mode",
        "kill_switch",
        "artifacts",
        "backup_digest",
    }
    if set(manifest) != required or manifest.get("kind") != BACKUP_KIND:
        raise RuntimeBackupError("runtime backup manifest fields or kind are invalid")
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise RuntimeBackupError("runtime backup schema is unsupported")
    if manifest.get("runtime_mode") != RuntimeMode.HALTED.value or manifest.get("kill_switch") is not True:
        raise RuntimeBackupError("runtime backup source was not HALTED with kill switch=true")
    if manifest.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest:
        raise RuntimeBackupError("runtime backup policy identity mismatch")
    if manifest.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest:
        raise RuntimeBackupError("runtime backup products identity mismatch")
    for key in (
        "runtime_identity_digest",
        "generic_checksum",
        "policy_checksum",
        "oi_checksum",
        "account_identity_digest",
        "account_epoch",
        "registry_checksum",
        "registry_binding_receipt_digest",
        "seed_digest",
        "deployment_checksum",
        "bundle_digest",
        "backup_digest",
    ):
        _sha64(manifest.get(key), label=key)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise RuntimeBackupError("runtime backup artifact manifest is empty")
    if set(artifacts) != set(members) - {"manifest.json"}:
        raise RuntimeBackupError("runtime backup artifact manifest/member set mismatch")
    for logical, meta in artifacts.items():
        if not _allowed_logical_member(logical):
            raise RuntimeBackupError(f"runtime backup contains non-allowlisted artifact: {logical}")
        if not isinstance(meta, dict) or set(meta) != {"role", "size", "sha256", "sequence", "checksum"}:
            raise RuntimeBackupError(f"runtime backup artifact metadata is invalid: {logical}")
        payload = members[logical]
        if type(meta["size"]) is not int or meta["size"] != len(payload):
            raise RuntimeBackupError(f"runtime backup artifact size mismatch: {logical}")
        if _sha64(meta["sha256"], label=f"artifact {logical} sha256") != sha256(payload).hexdigest():
            raise RuntimeBackupError(f"runtime backup artifact digest mismatch: {logical}")
    unsigned = {key: value for key, value in manifest.items() if key != "backup_digest"}
    if manifest["backup_digest"] != _backup_digest(unsigned):
        raise RuntimeBackupError("runtime backup total digest mismatch")
    if members["manifest.json"] != canonical_json_bytes(manifest):
        raise RuntimeBackupError("runtime backup manifest is not canonical JSON")


def _materialize_for_verification(
    members: dict[str, bytes], temp: Path, manifest: dict[str, Any]
) -> tuple[Path, Path, Path]:
    runtime = temp / "runtime"
    registry_root = temp / "registry"
    runtime.mkdir()
    registry_root.mkdir()
    registry_path = registry_root / "account-runtime-registry.json"
    for logical, payload in members.items():
        if logical == "manifest.json":
            continue
        parts = PurePosixPath(logical).parts
        if parts[0] == "runtime":
            target = runtime.joinpath(*parts[1:])
        elif parts[:2] == ("registry", "nonce-ledger"):
            target = registry_root / "account-runtime-registry.json.nonce-ledger"
            target = target.joinpath(*parts[2:])
        elif parts[0] == "registry":
            suffix = parts[1].removeprefix("account-runtime-registry.json")
            target = registry_path.with_name(registry_path.name + suffix)
        else:
            raise RuntimeBackupError("runtime backup logical member root is invalid")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    state_name = Path(str(manifest["source_runtime_path"])) / "state.json"
    # The configured state filename is normally state.json; locate the generic envelope if customized.
    state_candidates = [runtime / "state.json"]
    if not state_candidates[0].exists():
        for item in runtime.iterdir():
            if item.is_file():
                try:
                    raw = json.loads(item.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(raw, dict) and "state" in raw and "sequence" in raw and "checksum" in raw:
                    state_candidates.append(item)
                    break
    del state_name
    return runtime, registry_path, state_candidates[-1]


def verify_backup_archive(path: str | Path) -> BackupVerification:
    """Verify archive safety, artifact checksums and cross-file runtime/account identity."""

    try:
        verified = verify_archive(
            path,
            max_member_bytes=BACKUP_MAX_MEMBER_BYTES,
            max_total_bytes=BACKUP_MAX_TOTAL_BYTES,
            max_members=BACKUP_MAX_MEMBERS,
        )
        members = dict(verified.members)
        if "manifest.json" not in members:
            raise RuntimeBackupError("runtime backup manifest is missing")
        if any(not _allowed_logical_member(name) for name in members):
            raise RuntimeBackupError("runtime backup contains an unknown artifact")
        try:
            manifest = json.loads(members["manifest.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeBackupError("runtime backup manifest is invalid JSON") from exc
        if not isinstance(manifest, dict):
            raise RuntimeBackupError("runtime backup manifest root is invalid")
        _validate_manifest(manifest, members)
        with tempfile.TemporaryDirectory(prefix="afuture-backup-verify-") as temp_name:
            runtime, registry_path, state_path = _materialize_for_verification(
                members, Path(temp_name), manifest
            )
            authority = _validate_runtime_authority(
                runtime=runtime,
                state_path=state_path,
                registry_path=registry_path,
            )
            # Staged paths differ from source paths; validate immutable semantic identities manually.
            expected = {
                key: manifest[key]
                for key in (
                    "generic_sequence",
                    "generic_checksum",
                    "policy_sequence",
                    "policy_checksum",
                    "oi_sequence",
                    "oi_checksum",
                    "account_identity_digest",
                    "account_epoch",
                    "seed_digest",
                    "policy_definition_digest",
                    "products_manifest_digest",
                    "deployment_sequence",
                    "deployment_checksum",
                    "bundle_digest",
                    "runtime_mode",
                    "kill_switch",
                )
            }
            observed = {key: authority[key] for key in expected}
            if observed != expected:
                raise RuntimeBackupError("runtime backup cross-file authority identity mismatch")
        return BackupVerification(manifest=manifest, archive_sha256=verified.archive_sha256)
    except RuntimeBackupError:
        raise
    except (OSError, RuntimeError, ValueError, SecureArchiveError) as exc:
        raise RuntimeBackupError(str(exc)) from exc


def _write_file_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("restore write made no progress")
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


def _directory_empty_or_missing(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise RuntimeBackupError(f"{label} symlink is forbidden")
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise RuntimeBackupError(f"{label} must be empty")


def _registry_final_paths_clear(path: Path) -> None:
    candidates = [
        path,
        path.with_name(path.name + ".prev"),
        path.with_name(path.name + ".lineage"),
        path.with_name(path.name + ".lock"),
        path.with_name(path.name + ".nonce-ledger"),
    ]
    if any(item.exists() or item.is_symlink() for item in candidates):
        raise RuntimeBackupError("restore refuses existing account registry authority")


def restore_runtime(
    *,
    backup_path: str | Path,
    runtime_dir: str | Path,
    account_registry_path: str | Path,
    registry_staging_path: str | Path,
) -> dict[str, object]:
    """Restore to exact empty canonical paths; never connect a Broker or grant RUNNING."""

    verification = verify_backup_archive(backup_path)
    manifest = verification.manifest
    runtime = canonical_safe_path(runtime_dir, label="restore runtime")
    registry = canonical_safe_path(account_registry_path, label="restore account registry")
    registry_stage = canonical_safe_path(registry_staging_path, label="registry restore staging")
    if str(runtime) != manifest["source_runtime_path"]:
        raise RuntimeBackupError("restore runtime path differs from backed-up canonical runtime")
    if str(registry) != manifest["source_account_registry_path"]:
        raise RuntimeBackupError("restore registry path differs from backed-up canonical registry")
    _directory_empty_or_missing(runtime, label="restore runtime")
    _directory_empty_or_missing(registry_stage, label="registry restore staging")
    _registry_final_paths_clear(registry)
    runtime.parent.mkdir(parents=True, exist_ok=True)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry_stage.parent.mkdir(parents=True, exist_ok=True)

    archive = verify_archive(
        backup_path,
        max_member_bytes=BACKUP_MAX_MEMBER_BYTES,
        max_total_bytes=BACKUP_MAX_TOTAL_BYTES,
        max_members=BACKUP_MAX_MEMBERS,
    )
    members = dict(archive.members)
    runtime_stage = Path(tempfile.mkdtemp(prefix=f".{runtime.name}.restore-", dir=runtime.parent))
    if registry_stage.exists():
        registry_stage.rmdir()
    registry_stage.mkdir(mode=0o700)
    staged_registry_file = registry_stage / registry.name
    published_runtime = False
    try:
        for logical, payload in members.items():
            if logical == "manifest.json":
                continue
            parts = PurePosixPath(logical).parts
            if parts[0] == "runtime":
                target = runtime_stage.joinpath(*parts[1:])
            elif parts[:2] == ("registry", "nonce-ledger"):
                target = registry_stage / (registry.name + ".nonce-ledger")
                target = target.joinpath(*parts[2:])
            elif parts[0] == "registry":
                suffix = parts[1].removeprefix("account-runtime-registry.json")
                target = staged_registry_file.with_name(staged_registry_file.name + suffix)
            else:
                raise RuntimeBackupError("restore archive member root is invalid")
            _write_file_exact(target, payload)

        # Validate staged registry and runtime before any authoritative publication.
        AccountRuntimeRegistry(staged_registry_file).load_required()
        state_path = runtime_stage / "state.json"
        if not state_path.exists():
            raise RuntimeBackupError("restore currently requires canonical state.json generic state")
        generic_store = StateStore(state_path)
        record = generic_store.load_required_record()
        if record.state.runtime_mode != RuntimeMode.HALTED.value or not record.state.kill_switch:
            raise RuntimeBackupError("restore source generic state lost HALTED/kill-switch invariant")
        record.state.runtime_mode = RuntimeMode.HALTED.value
        record.state.kill_switch = True
        record.state.metadata_verified = False
        record.state.kill_reason = "runtime restored from verified backup; Doctor and fresh permit required"
        generic_store.save(
            record.state,
            expected_sequence=record.sequence,
            expected_checksum=record.checksum,
        )
        permit_store = Stress90ActivationPermitStore(
            runtime_stage / "stress90_activation_permit.json"
        )
        if permit_store.path.exists() or permit_store.previous_path.exists():
            permit_store.invalidate("runtime restored from backup; fresh Doctor permit required")
        _validate_runtime_authority(
            runtime=runtime_stage,
            state_path=state_path,
            registry_path=staged_registry_file,
        )
        _fsync_dir(runtime_stage)
        _fsync_dir(registry_stage)

        # Publish registry evidence with current last, so partial publication remains fail-closed.
        staged_ledger = registry_stage / (registry.name + ".nonce-ledger")
        final_ledger = registry.with_name(registry.name + ".nonce-ledger")
        if staged_ledger.exists():
            os.rename(staged_ledger, final_ledger)
            _fsync_dir(registry.parent)
        for suffix in (".lineage", ".prev", ".lock"):
            source = staged_registry_file.with_name(staged_registry_file.name + suffix)
            if source.exists():
                os.rename(source, registry.with_name(registry.name + suffix))
                _fsync_dir(registry.parent)
        os.rename(staged_registry_file, registry)
        _fsync_dir(registry.parent)

        if runtime.exists():
            if any(runtime.iterdir()):
                raise RuntimeBackupError("restore runtime became non-empty before publish")
            runtime.rmdir()
            _fsync_dir(runtime.parent)
        os.rename(runtime_stage, runtime)
        published_runtime = True
        _fsync_dir(runtime.parent)
        registry_stage.rmdir()
        _fsync_dir(registry_stage.parent)

        final_state = StateStore(runtime / "state.json").load_required_record().state
        final_policy = Stress90PolicyStateStore(runtime / "stress90_policy_state.json").load_required()
        final_registry = AccountRuntimeRegistry(registry).require_binding_evidence(
            final_policy.live_account_identity_digest or "",
            runtime,
            final_policy.live_account_epoch or "",
        )
        del final_registry
        if final_state.runtime_mode != RuntimeMode.HALTED.value or not final_state.kill_switch:
            raise RuntimeBackupError("restored runtime did not remain HALTED and kill-switched")
        permit = Stress90ActivationPermitStore(
            runtime / "stress90_activation_permit.json"
        ).load_record()
        if permit is not None and permit.permit.status == "issued":
            raise RuntimeBackupError("restored runtime retained an issued activation permit")
        return {
            "restored": True,
            "runtime_dir": str(runtime),
            "account_registry_path": str(registry),
            "runtime_mode": RuntimeMode.HALTED.value,
            "kill_switch": True,
            "metadata_verified": False,
            "activation_permit_valid": False,
            "orders_sent": 0,
            "cancels_sent": 0,
            "next_steps": ["status", "deployment-verify", "doctor", "issue-fresh-permit"],
        }
    except Exception:
        # Never remove or mutate the source backup.  Hidden staging paths are not canonical runtimes.
        if not published_runtime and runtime_stage.exists():
            shutil.rmtree(runtime_stage, ignore_errors=True)
        raise
    finally:
        if registry_stage.exists() and not any(registry_stage.iterdir()):
            try:
                registry_stage.rmdir()
            except OSError:
                pass
