"""Machine-local deployment identity sealing and verification for Stress-90."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .directional_stress90_policy import STRESS90_POLICY
from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
from .provenance import (
    ProvenanceError,
    canonical_safe_path,
    file_sha256,
    git_head,
    interpreter_identity,
    native_module_identity,
    production_source_tree_digest,
    repository_root,
    require_clean_tracked_worktree,
)
from .secure_archive import canonical_json_bytes
from .stress90_bootstrap_bundle import (
    BootstrapBundleError,
    verify_stress90_bundle,
)
from .stress90_risk_overlay import stress90_risk_overlay_digest

DEPLOYMENT_IDENTITY_KIND = "afuture.deployment-identity"
DEPLOYMENT_IDENTITY_SCHEMA_VERSION = 1
DEPLOYMENT_IDENTITY_FILENAME = "deployment_identity.json"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_SECRET_KEY_FRAGMENTS = (
    "password",
    "authcode",
    "auth_code",
    "webhook",
    "ctp_user",
    "username",
    "investor_id",
    "invest_unit_id",
    "account_id",
)


class DeploymentIdentityError(RuntimeError):
    """Deployment identity cannot be trusted or does not match the local deployment."""


@dataclass(frozen=True)
class DeploymentIdentityRecord:
    identity: dict[str, Any]
    sequence: int
    parent_checksum: str | None
    checksum: str


@dataclass(frozen=True)
class DeploymentVerification:
    passed: bool
    seal_sequence: int
    seal_checksum: str
    changes: tuple[str, ...]
    expected: dict[str, Any]
    current: dict[str, Any]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "seal_sequence": self.seal_sequence,
            "seal_checksum": self.seal_checksum,
            "changes": list(self.changes),
            "expected": self.expected,
            "current": self.current,
            "orders_sent": 0,
            "cancels_sent": 0,
        }


def _reject_secrets(value: object, *, path: str = "identity") -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).lower()
            if any(fragment in key for fragment in _SECRET_KEY_FRAGMENTS):
                raise DeploymentIdentityError(
                    f"deployment identity secret-like field is forbidden: {path}.{raw_key}"
                )
            _reject_secrets(item, path=f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secrets(item, path=f"{path}[{index}]")


def _digest_unsigned(unsigned: dict[str, Any]) -> str:
    return sha256(canonical_json_bytes(unsigned)).hexdigest()


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DeploymentIdentityError("deployment identity file is missing") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DeploymentIdentityError("deployment identity path must be a regular non-symlink file")
    try:
        payload = path.read_bytes()
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except DeploymentIdentityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentIdentityError("deployment identity is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise DeploymentIdentityError("deployment identity envelope must be an object")
    return raw


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentIdentityError(f"duplicate deployment identity JSON key: {key}")
        result[key] = value
    return result


class DeploymentIdentityStore:
    """Atomic current/.prev deployment seal; `.prev` is never automatic recovery input."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(self.path.name + ".prev")

    def load_record(self) -> DeploymentIdentityRecord | None:
        if not self.path.exists() and not self.path.is_symlink():
            if self.previous_path.exists() or self.previous_path.is_symlink():
                raise DeploymentIdentityError(
                    "deployment identity current is missing while .prev exists"
                )
            return None
        current = self._decode(_read_json_file(self.path))
        self._validate_previous(current)
        return current

    def load_required(self) -> DeploymentIdentityRecord:
        record = self.load_record()
        if record is None:
            raise DeploymentIdentityError("required deployment identity seal is missing")
        return record

    def seal(self, identity: dict[str, Any]) -> DeploymentIdentityRecord:
        if not isinstance(identity, dict) or not identity:
            raise DeploymentIdentityError("deployment identity payload must be a non-empty object")
        _reject_secrets(identity)
        try:
            canonical_json_bytes(identity)
        except Exception as exc:
            raise DeploymentIdentityError(
                "deployment identity payload is not canonical JSON"
            ) from exc
        current = self.load_record()
        sequence = 1 if current is None else current.sequence + 1
        parent = None if current is None else current.checksum
        unsigned: dict[str, Any] = {
            "kind": DEPLOYMENT_IDENTITY_KIND,
            "schema_version": DEPLOYMENT_IDENTITY_SCHEMA_VERSION,
            "sequence": sequence,
            "parent_checksum": parent,
            "identity": identity,
        }
        record = DeploymentIdentityRecord(
            identity=dict(identity),
            sequence=sequence,
            parent_checksum=parent,
            checksum=_digest_unsigned(unsigned),
        )
        encoded = canonical_json_bytes({**unsigned, "checksum": record.checksum})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if current is not None:
            self._atomic_replace(self.previous_path, self.path.read_bytes())
        self._atomic_replace(self.path, encoded)
        reloaded = self.load_required()
        if reloaded != record:
            raise DeploymentIdentityError("deployment identity changed during durable seal")
        return reloaded

    def _decode(self, raw: dict[str, Any]) -> DeploymentIdentityRecord:
        fields = {
            "kind",
            "schema_version",
            "sequence",
            "parent_checksum",
            "identity",
            "checksum",
        }
        if set(raw) != fields or raw.get("kind") != DEPLOYMENT_IDENTITY_KIND:
            raise DeploymentIdentityError("deployment identity envelope fields or kind are invalid")
        if raw.get("schema_version") != DEPLOYMENT_IDENTITY_SCHEMA_VERSION:
            raise DeploymentIdentityError("deployment identity schema is unsupported")
        sequence = raw.get("sequence")
        if type(sequence) is not int or sequence <= 0:
            raise DeploymentIdentityError("deployment identity sequence is invalid")
        parent = raw.get("parent_checksum")
        if sequence == 1:
            if parent is not None:
                raise DeploymentIdentityError("initial deployment identity parent is invalid")
        elif not isinstance(parent, str) or _HEX64.fullmatch(parent) is None:
            raise DeploymentIdentityError("deployment identity parent checksum is invalid")
        identity = raw.get("identity")
        if not isinstance(identity, dict) or not identity:
            raise DeploymentIdentityError("deployment identity payload is invalid")
        _reject_secrets(identity)
        checksum = raw.get("checksum")
        if not isinstance(checksum, str) or _HEX64.fullmatch(checksum) is None:
            raise DeploymentIdentityError("deployment identity checksum is invalid")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != _digest_unsigned(unsigned):
            raise DeploymentIdentityError("deployment identity checksum mismatch")
        return DeploymentIdentityRecord(dict(identity), sequence, parent, checksum)

    def _validate_previous(self, current: DeploymentIdentityRecord) -> None:
        if not self.previous_path.exists() and not self.previous_path.is_symlink():
            if current.sequence != 1:
                raise DeploymentIdentityError("deployment identity .prev evidence is missing")
            return
        previous = self._decode(_read_json_file(self.previous_path))
        if previous.sequence == current.sequence and previous.checksum == current.checksum:
            return
        if (
            previous.sequence + 1 != current.sequence
            or current.parent_checksum != previous.checksum
        ):
            raise DeploymentIdentityError("deployment identity predecessor chain mismatch")

    @staticmethod
    def _atomic_replace(target: Path, payload: bytes) -> None:
        if target.is_symlink():
            raise DeploymentIdentityError("deployment identity target symlink is forbidden")
        temporary: Path | None = None
        try:
            with NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
            directory = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise DeploymentIdentityError("deployment identity durable replace failed") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def deployment_runtime(config, *, shadow_account: bool = False) -> Path:
    base = canonical_safe_path(Path(config.state_path).parent, label="runtime")
    return canonical_safe_path(base / "shadow", label="Shadow runtime") if shadow_account else base


def _deployment_payload(
    *,
    config,
    config_path: str | Path,
    runtime_dir: str | Path,
    account_registry_path: str | Path,
    bundle_path: str | Path,
    repo_dir: str | Path | None = None,
    deployment_role: str,
) -> dict[str, Any]:
    repo = repository_root(repo_dir)
    source_commit = git_head(repo)
    source_digest, _entries = production_source_tree_digest(repo)
    runtime = canonical_safe_path(runtime_dir, label="runtime")
    registry = canonical_safe_path(account_registry_path, label="account registry")
    bundle = canonical_safe_path(bundle_path, label="bootstrap bundle")
    bundle_verification = verify_stress90_bundle(bundle, require_production=True, repo_dir=repo)
    seed = Stress90SeedStore(runtime / "stress90_bootstrap_seed.json").load_required()
    policy = Stress90PolicyStateStore(runtime / "stress90_policy_state.json").load_required().state
    if (
        seed.seed_digest != bundle_verification.manifest["seed_digest"]
        or policy.bootstrap_seed_digest != seed.seed_digest
        or policy.policy_definition_digest != STRESS90_POLICY.policy_definition_digest
        or policy.products_manifest_digest != STRESS90_POLICY.products_manifest_digest
    ):
        raise DeploymentIdentityError("deployment runtime does not match verified bootstrap bundle")
    if deployment_role not in {"live", "shadow"}:
        raise DeploymentIdentityError("deployment role must be live or shadow")
    environment = interpreter_identity()
    return {
        "source_commit": source_commit,
        "production_source_tree_digest": source_digest,
        "production_config_sha256": file_sha256(config_path),
        "core_constraints_sha256": file_sha256(repo / "constraints/core-dev.txt"),
        "live_constraints_sha256": file_sha256(repo / "constraints/live.txt"),
        **environment,
        "repository_path": str(repo),
        "runtime_path": str(runtime),
        "account_registry_path": str(registry),
        "bundle_path": str(bundle),
        "bundle_digest": bundle_verification.bundle_digest,
        "bundle_archive_sha256": bundle_verification.archive_sha256,
        "seed_digest": seed.seed_digest,
        "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
        "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
        "risk_overlay_digest": stress90_risk_overlay_digest(config.directional, config.risk),
        "account_continuity_mode": config.directional.account_continuity_mode,
        "deployment_role": deployment_role,
        "vnpy_ctp": native_module_identity("vnpy_ctp"),
    }


def seal_deployment(
    *,
    config,
    config_path: str | Path,
    bundle_path: str | Path,
    runtime_dir: str | Path,
    account_registry_path: str | Path,
    repo_dir: str | Path | None = None,
    deployment_role: str = "live",
) -> DeploymentIdentityRecord:
    """Seal a clean tracked checkout and invalidate any old technical permit."""

    try:
        repo = repository_root(repo_dir)
        require_clean_tracked_worktree(repo)
        payload = _deployment_payload(
            config=config,
            config_path=config_path,
            runtime_dir=runtime_dir,
            account_registry_path=account_registry_path,
            bundle_path=bundle_path,
            repo_dir=repo,
            deployment_role=deployment_role,
        )
        if _HEX40.fullmatch(payload["source_commit"]) is None:
            raise DeploymentIdentityError("deployment source commit is invalid")
        runtime = canonical_safe_path(runtime_dir, label="runtime")
        permit_path = runtime / "stress90_activation_permit.json"
        if permit_path.exists() or (runtime / "stress90_activation_permit.json.prev").exists():
            from .stress90_activation_permit import Stress90ActivationPermitStore

            Stress90ActivationPermitStore(permit_path).invalidate(
                "deployment identity seal changed; a fresh Doctor permit is required"
            )
        return DeploymentIdentityStore(runtime / DEPLOYMENT_IDENTITY_FILENAME).seal(payload)
    except DeploymentIdentityError:
        raise
    except (BootstrapBundleError, ProvenanceError, OSError, RuntimeError, ValueError) as exc:
        raise DeploymentIdentityError(str(exc)) from exc


def verify_deployment(
    *,
    config,
    config_path: str | Path,
    runtime_dir: str | Path,
    account_registry_path: str | Path,
    repo_dir: str | Path | None = None,
) -> DeploymentVerification:
    """Compare every deployment-bound local identity without modifying state."""

    try:
        runtime = canonical_safe_path(runtime_dir, label="runtime")
        record = DeploymentIdentityStore(runtime / DEPLOYMENT_IDENTITY_FILENAME).load_required()
        expected = record.identity
        bundle_path = expected.get("bundle_path")
        role = expected.get("deployment_role")
        if not isinstance(bundle_path, str) or role not in {"live", "shadow"}:
            raise DeploymentIdentityError("deployment seal bundle/role identity is invalid")
        current = _deployment_payload(
            config=config,
            config_path=config_path,
            runtime_dir=runtime,
            account_registry_path=account_registry_path,
            bundle_path=bundle_path,
            repo_dir=repo_dir,
            deployment_role=role,
        )
        changes = tuple(
            key
            for key in sorted(set(expected) | set(current))
            if expected.get(key) != current.get(key)
        )
        return DeploymentVerification(
            passed=not changes,
            seal_sequence=record.sequence,
            seal_checksum=record.checksum,
            changes=changes,
            expected=dict(expected),
            current=current,
        )
    except DeploymentIdentityError:
        raise
    except (BootstrapBundleError, ProvenanceError, OSError, RuntimeError, ValueError) as exc:
        raise DeploymentIdentityError(str(exc)) from exc


def require_matching_deployment(
    *,
    config,
    config_path: str | Path,
    runtime_dir: str | Path,
    account_registry_path: str | Path,
    expected_role: str,
    repo_dir: str | Path | None = None,
) -> DeploymentVerification:
    verification = verify_deployment(
        config=config,
        config_path=config_path,
        runtime_dir=runtime_dir,
        account_registry_path=account_registry_path,
        repo_dir=repo_dir,
    )
    actual_role = verification.expected.get("deployment_role")
    if actual_role != expected_role:
        raise DeploymentIdentityError(
            f"deployment seal role mismatch: expected {expected_role}, sealed {actual_role}"
        )
    if not verification.passed:
        raise DeploymentIdentityError(
            "deployment identity mismatch: " + ", ".join(verification.changes)
        )
    return verification
