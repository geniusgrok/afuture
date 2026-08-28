"""Verifiable, deterministic Stress-90 bootstrap bundle creation and installation."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .directional_activity import DirectionalActivityStore, validate_directional_activity_snapshot
from .directional_ohlc_cache import DirectionalOHLCCacheStore
from .directional_stress90_bootstrap import (
    FIXED_STRESS90_INPUT_SHA256,
    OFFICIAL_STRESS90_BOOTSTRAP_EXPECTATIONS,
    bootstrap_stress90,
)
from .directional_stress90_oi_runtime import (
    FIXED_HISTORICAL_60M_SOURCE,
    Stress90OiEvidenceStore,
)
from .directional_stress90_policy import STRESS90_POLICY
from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
from .provenance import (
    ProvenanceError,
    canonical_safe_path,
    file_sha256,
    git_commit_is_ancestor,
    git_head,
    repository_root,
)
from .secure_archive import (
    SecureArchiveError,
    build_deterministic_archive,
    canonical_json_bytes,
    verify_archive,
)

BUNDLE_KIND = "afuture.stress90.bootstrap-bundle"
BUNDLE_SCHEMA_VERSION = 1
BUNDLE_MAX_MEMBER_BYTES = 128 * 1024 * 1024
BUNDLE_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX40 = re.compile(r"[0-9a-f]{40}")

_RUNTIME_MEMBERS = (
    "directional_activity.json",
    "directional_ohlc_cache.json",
    "stress90_bootstrap_seed.json",
    "stress90_oi_evidence.json",
    "stress90_oi_evidence.json.lineage",
    "stress90_policy_state.json",
)
_BUNDLE_MEMBERS = frozenset(("manifest.json", *_RUNTIME_MEMBERS))
_FORBIDDEN_BOUND_ARTIFACTS = (
    "state.json",
    "state.json.prev",
    "stress90_activation_permit.json",
    "stress90_activation_permit.json.prev",
    "stress90_ctp_orders.json",
    "stress90_ctp_session_evidence.json",
    "stress90_execution_intent.json",
    "stress90_lifecycle_transaction.json",
    "stress90_lifecycle_transaction.json.prev",
    "stress90_operator_continuity.json",
    "stress90_crash_fill_recovery.json",
    "ctp_trading_day_evidence.json",
)


class BootstrapBundleError(RuntimeError):
    """Bootstrap bundle identity, archive bytes, or install target is unsafe."""


@dataclass(frozen=True)
class BootstrapBundleVerification:
    manifest: dict[str, Any]
    archive_sha256: str
    production_ready: bool

    @property
    def bundle_digest(self) -> str:
        return str(self.manifest["bundle_digest"])


def _decode_json(payload: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise BootstrapBundleError(f"duplicate {label} key: {key}")
            result[key] = value
        return result

    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except BootstrapBundleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapBundleError(f"{label} is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise BootstrapBundleError(f"{label} must be a JSON object")
    return raw


def _hex64(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise BootstrapBundleError(f"{label} must be lowercase SHA-256")
    return value


def _hex40(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _HEX40.fullmatch(value) is None:
        raise BootstrapBundleError(f"{label} must be a full Git commit")
    return value


def _member_manifest(members: dict[str, bytes]) -> dict[str, dict[str, object]]:
    return {
        name: {"sha256": sha256(payload).hexdigest(), "size": len(payload)}
        for name, payload in sorted(members.items())
    }


def _constraints(repo: Path) -> dict[str, str]:
    return {
        "constraints/core-dev.txt": file_sha256(repo / "constraints/core-dev.txt"),
        "constraints/live.txt": file_sha256(repo / "constraints/live.txt"),
    }


def _bundle_digest(unsigned_manifest: dict[str, Any]) -> str:
    return sha256(canonical_json_bytes(unsigned_manifest)).hexdigest()


def _require_clean_bootstrap(runtime: Path) -> tuple[Any, Any, Any]:
    for name in _FORBIDDEN_BOUND_ARTIFACTS:
        path = runtime / name
        if path.exists() or path.is_symlink():
            raise BootstrapBundleError(
                f"production bundle requires never-bound clean bootstrap state: {name} exists"
            )
    policy_store = Stress90PolicyStateStore(runtime / "stress90_policy_state.json")
    if policy_store.previous_path.exists() or policy_store.previous_path.is_symlink():
        raise BootstrapBundleError("clean bootstrap policy state cannot have .prev evidence")
    seed = Stress90SeedStore(runtime / "stress90_bootstrap_seed.json").load_required(
        expected_source_manifest=FIXED_STRESS90_INPUT_SHA256
    )
    policy_record = policy_store.load_required_record()
    policy = policy_record.state
    if policy_record.sequence != 1:
        raise BootstrapBundleError("clean bootstrap policy state must be sequence 1")
    if (
        policy.live_account_identity_digest is not None
        or policy.live_account_epoch is not None
        or policy.live_inception_day is not None
        or policy.live_inception_equity is not None
        or policy.last_completed_account_day is not None
        or policy.completed_account_wealth != 1.0
        or policy.completed_account_high_watermark != 1.0
    ):
        raise BootstrapBundleError(
            "production bundle policy state is already bound to live account path"
        )
    if policy.bootstrap_seed_digest != seed.seed_digest:
        raise BootstrapBundleError("bootstrap seed/policy identity mismatch")
    oi_store = Stress90OiEvidenceStore(runtime / "stress90_oi_evidence.json")
    oi_record = oi_store.load_required_record()
    if oi_record.sequence != 1 or oi_record.parent_checksum is not None:
        raise BootstrapBundleError("clean bootstrap OI evidence must be sequence 1")
    if oi_store.previous_path.exists() or oi_store.previous_path.is_symlink():
        raise BootstrapBundleError("clean bootstrap OI evidence cannot have .prev evidence")
    if len(oi_record.state.completed) != 1 or oi_record.state.in_progress is not None:
        raise BootstrapBundleError("bootstrap OI evidence is not a clean historical bridge")
    completed = oi_record.state.completed[0]
    if (
        completed.source != FIXED_HISTORICAL_60M_SOURCE
        or completed.trading_day != seed.bootstrap_through_day
        or not completed.complete
    ):
        raise BootstrapBundleError("bootstrap historical OI evidence identity mismatch")
    return seed, policy_record, oi_record


def create_stress90_bundle(
    *,
    runtime_dir: str | Path,
    output_path: str | Path,
    repo_dir: str | Path | None = None,
) -> BootstrapBundleVerification:
    """Create a production bundle only from the official fixed historical bootstrap."""

    try:
        runtime = canonical_safe_path(runtime_dir, label="bootstrap runtime")
        repo = repository_root(repo_dir)
        source_commit = git_head(repo)
        seed, policy_record, oi_record = _require_clean_bootstrap(runtime)
        parity = bootstrap_stress90(
            runtime_dir=runtime,
            through_day=seed.bootstrap_through_day,
            expectations=OFFICIAL_STRESS90_BOOTSTRAP_EXPECTATIONS,
            write_artifacts=False,
        )
        if (
            parity.historical_candidate_parity is not True
            or parity.candidate_weight_sha256 != STRESS90_POLICY.historical_candidate_weight_sha256
            or parity.policy_definition_digest != STRESS90_POLICY.policy_definition_digest
            or dict(parity.source_manifest) != dict(FIXED_STRESS90_INPUT_SHA256)
        ):
            raise BootstrapBundleError("official Stress-90 bootstrap parity is not exact")
        if policy_record.state.policy_definition_digest != STRESS90_POLICY.policy_definition_digest:
            raise BootstrapBundleError("bootstrap policy definition identity mismatch")
        if policy_record.state.products_manifest_digest != STRESS90_POLICY.products_manifest_digest:
            raise BootstrapBundleError("bootstrap products manifest identity mismatch")
        if oi_record.state.completed[0].trading_day != parity.last_target_day:
            raise BootstrapBundleError("bootstrap OI/target day identity mismatch")

        payload_members: dict[str, bytes] = {}
        for name in _RUNTIME_MEMBERS:
            path = runtime / name
            if not path.exists() or path.is_symlink() or not path.is_file():
                raise BootstrapBundleError(f"required clean bootstrap artifact is missing: {name}")
            payload_members[name] = path.read_bytes()
        member_manifest = _member_manifest(payload_members)
        unsigned: dict[str, Any] = {
            "kind": BUNDLE_KIND,
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "production_ready": True,
            "source_commit": source_commit,
            "constraints": _constraints(repo),
            "source_sha256": dict(sorted(FIXED_STRESS90_INPUT_SHA256.items())),
            "candidate_weight_sha256": STRESS90_POLICY.historical_candidate_weight_sha256,
            "historical_candidate_parity": True,
            "base_max_abs_error": parity.base_max_abs_error,
            "batch_incremental_max_abs_error": parity.batch_incremental_max_abs_error,
            "seed_digest": seed.seed_digest,
            "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
            "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
            "products": list(STRESS90_POLICY.products),
            "oi_products": list(STRESS90_POLICY.oi_products),
            "bootstrap_through_day": seed.bootstrap_through_day,
            "target_day_range": {
                "first": parity.first_target_day,
                "last": parity.last_target_day,
                "count": parity.target_day_count,
            },
            "members": member_manifest,
        }
        manifest = {**unsigned, "bundle_digest": _bundle_digest(unsigned)}
        members = {"manifest.json": canonical_json_bytes(manifest), **payload_members}
        build_deterministic_archive(
            output_path,
            members,
            max_member_bytes=BUNDLE_MAX_MEMBER_BYTES,
            max_total_bytes=BUNDLE_MAX_TOTAL_BYTES,
            max_members=len(_BUNDLE_MEMBERS),
        )
        return verify_stress90_bundle(
            output_path,
            require_production=True,
            repo_dir=repo,
        )
    except BootstrapBundleError:
        raise
    except (OSError, ValueError, SecureArchiveError, ProvenanceError, RuntimeError) as exc:
        raise BootstrapBundleError(str(exc)) from exc


def _validate_manifest(
    manifest: dict[str, Any],
    members: dict[str, bytes],
    *,
    require_production: bool,
) -> None:
    fields = {
        "kind",
        "schema_version",
        "production_ready",
        "source_commit",
        "constraints",
        "source_sha256",
        "candidate_weight_sha256",
        "historical_candidate_parity",
        "base_max_abs_error",
        "batch_incremental_max_abs_error",
        "seed_digest",
        "policy_definition_digest",
        "products_manifest_digest",
        "products",
        "oi_products",
        "bootstrap_through_day",
        "target_day_range",
        "members",
        "bundle_digest",
    }
    if set(manifest) != fields or manifest.get("kind") != BUNDLE_KIND:
        raise BootstrapBundleError("bootstrap bundle manifest fields or kind are invalid")
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise BootstrapBundleError("bootstrap bundle schema is unsupported")
    production_ready = manifest.get("production_ready")
    if not isinstance(production_ready, bool) or (require_production and not production_ready):
        raise BootstrapBundleError("bootstrap bundle is not production-ready")
    if production_ready:
        _hex40(manifest["source_commit"], label="bundle source commit")
        if manifest.get("historical_candidate_parity") is not True:
            raise BootstrapBundleError("production bundle historical parity is false")
        if (
            manifest.get("candidate_weight_sha256")
            != STRESS90_POLICY.historical_candidate_weight_sha256
        ):
            raise BootstrapBundleError("production bundle candidate identity mismatch")
        if manifest.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest:
            raise BootstrapBundleError("production bundle policy identity mismatch")
        if manifest.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest:
            raise BootstrapBundleError("production bundle products identity mismatch")
        if tuple(manifest.get("products", ())) != STRESS90_POLICY.products:
            raise BootstrapBundleError("production bundle 50-product manifest mismatch")
        if tuple(manifest.get("oi_products", ())) != STRESS90_POLICY.oi_products:
            raise BootstrapBundleError("production bundle OI-product manifest mismatch")
        if manifest.get("source_sha256") != dict(sorted(FIXED_STRESS90_INPUT_SHA256.items())):
            raise BootstrapBundleError("production bundle source SHA manifest mismatch")
    member_meta = manifest.get("members")
    if not isinstance(member_meta, dict) or set(member_meta) != set(_RUNTIME_MEMBERS):
        raise BootstrapBundleError("bootstrap bundle member manifest is invalid")
    for name in _RUNTIME_MEMBERS:
        meta = member_meta.get(name)
        if not isinstance(meta, dict) or set(meta) != {"sha256", "size"}:
            raise BootstrapBundleError(f"bundle member metadata is invalid: {name}")
        digest = _hex64(meta["sha256"], label=f"bundle member {name} digest")
        size = meta["size"]
        payload = members[name]
        if type(size) is not int or size < 0 or size != len(payload):
            raise BootstrapBundleError(f"bundle member size mismatch: {name}")
        if digest != sha256(payload).hexdigest():
            raise BootstrapBundleError(f"bundle member digest mismatch: {name}")
    unsigned = {key: value for key, value in manifest.items() if key != "bundle_digest"}
    if _hex64(manifest["bundle_digest"], label="bundle digest") != _bundle_digest(unsigned):
        raise BootstrapBundleError("bootstrap bundle total digest mismatch")
    if canonical_json_bytes(manifest) != members["manifest.json"]:
        raise BootstrapBundleError("bootstrap bundle manifest is not canonical JSON")


def _validate_artifacts_in_directory(runtime: Path, manifest: dict[str, Any]) -> None:
    seed = Stress90SeedStore(runtime / "stress90_bootstrap_seed.json").load_required(
        expected_source_manifest=FIXED_STRESS90_INPUT_SHA256
    )
    policy_record = Stress90PolicyStateStore(
        runtime / "stress90_policy_state.json"
    ).load_required_record()
    oi_record = Stress90OiEvidenceStore(
        runtime / "stress90_oi_evidence.json"
    ).load_required_record()
    ohlc = DirectionalOHLCCacheStore(runtime / "directional_ohlc_cache.json").load(
        STRESS90_POLICY.products
    )
    activity = DirectionalActivityStore(runtime / "directional_activity.json").load()
    if ohlc is None or activity is None:
        raise BootstrapBundleError("bundle OHLC/activity evidence is missing")
    validate_directional_activity_snapshot(activity)
    policy = policy_record.state
    if (
        seed.seed_digest != manifest["seed_digest"]
        or seed.policy_definition_digest != STRESS90_POLICY.policy_definition_digest
        or seed.products_manifest_digest != STRESS90_POLICY.products_manifest_digest
        or policy.bootstrap_seed_digest != seed.seed_digest
        or policy.live_account_identity_digest is not None
        or policy.live_account_epoch is not None
        or policy.live_inception_day is not None
        or policy.live_inception_equity is not None
        or policy_record.sequence != 1
        or oi_record.sequence != 1
        or oi_record.parent_checksum is not None
        or len(oi_record.state.completed) != 1
        or oi_record.state.in_progress is not None
        or oi_record.state.completed[0].source != FIXED_HISTORICAL_60M_SOURCE
        or oi_record.state.completed[0].trading_day != seed.bootstrap_through_day
        or activity.trading_day != seed.bootstrap_through_day
    ):
        raise BootstrapBundleError("bundle seed/policy/OI/activity identity is not clean bootstrap")


def verify_stress90_bundle(
    path: str | Path,
    *,
    require_production: bool = True,
    repo_dir: str | Path | None = None,
) -> BootstrapBundleVerification:
    """Verify bundle bytes and current-code compatibility without modifying the bundle."""

    try:
        verified = verify_archive(
            path,
            allowed_members=_BUNDLE_MEMBERS,
            required_members=_BUNDLE_MEMBERS,
            max_member_bytes=BUNDLE_MAX_MEMBER_BYTES,
            max_total_bytes=BUNDLE_MAX_TOTAL_BYTES,
            max_members=len(_BUNDLE_MEMBERS),
        )
        members = dict(verified.members)
        manifest = _decode_json(members["manifest.json"], label="bootstrap bundle manifest")
        _validate_manifest(manifest, members, require_production=require_production)
        production_ready = bool(manifest["production_ready"])
        if production_ready:
            repo = repository_root(repo_dir)
            current_head = git_head(repo)
            source_commit = _hex40(manifest["source_commit"], label="bundle source commit")
            if not git_commit_is_ancestor(repo, source_commit, current_head):
                raise BootstrapBundleError(
                    "bundle source commit is not compatible with current checkout"
                )
            if manifest["constraints"] != _constraints(repo):
                raise BootstrapBundleError("bundle constraints differ from current implementation")

        with tempfile.TemporaryDirectory(prefix="afuture-bundle-verify-") as temp:
            runtime = Path(temp)
            for name in _RUNTIME_MEMBERS:
                (runtime / name).write_bytes(members[name])
            _validate_artifacts_in_directory(runtime, manifest)
        return BootstrapBundleVerification(
            manifest=manifest,
            archive_sha256=verified.archive_sha256,
            production_ready=production_ready,
        )
    except BootstrapBundleError:
        raise
    except (OSError, ValueError, SecureArchiveError, ProvenanceError, RuntimeError) as exc:
        raise BootstrapBundleError(str(exc)) from exc


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise BootstrapBundleError(f"bundle install path contains symlink component: {current}")


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_member(path: Path, payload: bytes) -> None:
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
                raise OSError("bundle install write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_stress90_bundle(
    *,
    bundle_path: str | Path,
    runtime_dir: str | Path,
    repo_dir: str | Path | None = None,
) -> dict[str, object]:
    """Install verified clean bootstrap artifacts through an atomic staging directory."""

    verification = verify_stress90_bundle(
        bundle_path,
        require_production=True,
        repo_dir=repo_dir,
    )
    runtime = canonical_safe_path(runtime_dir, label="bundle install runtime")
    _reject_symlink_components(runtime)
    if runtime.exists():
        if not runtime.is_dir() or runtime.is_symlink():
            raise BootstrapBundleError("bundle install target must be an empty directory")
        if any(runtime.iterdir()):
            raise BootstrapBundleError("bundle install refuses non-empty runtime")
    runtime.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(runtime.parent)
    staging = Path(tempfile.mkdtemp(prefix=f".{runtime.name}.install-", dir=runtime.parent))
    published = False
    failed_incident: Path | None = None
    try:
        archive = verify_archive(
            bundle_path,
            allowed_members=_BUNDLE_MEMBERS,
            required_members=_BUNDLE_MEMBERS,
            max_member_bytes=BUNDLE_MAX_MEMBER_BYTES,
            max_total_bytes=BUNDLE_MAX_TOTAL_BYTES,
            max_members=len(_BUNDLE_MEMBERS),
        )
        for name in _RUNTIME_MEMBERS:
            target = staging / name
            _write_member(target, archive.members[name])
        _fsync_dir(staging)
        _validate_artifacts_in_directory(staging, verification.manifest)
        _fsync_dir(staging)
        if runtime.exists():
            if any(runtime.iterdir()):
                raise BootstrapBundleError("bundle install target changed before publish")
            runtime.rmdir()
            _fsync_dir(runtime.parent)
        os.rename(staging, runtime)
        published = True
        _fsync_dir(runtime.parent)
        _validate_artifacts_in_directory(runtime, verification.manifest)
        return {
            "installed": True,
            "runtime_dir": str(runtime),
            "bundle_digest": verification.bundle_digest,
            "archive_sha256": verification.archive_sha256,
            "account_bound": False,
            "activation_permit_created": False,
            "runtime_mode": "UNINITIALIZED",
            "orders_sent": 0,
            "cancels_sent": 0,
        }
    except Exception:
        if published and runtime.exists():
            failed_incident = runtime.with_name(
                f".{runtime.name}.failed-install-{verification.archive_sha256[:16]}"
            )
            if not failed_incident.exists():
                try:
                    os.rename(runtime, failed_incident)
                    _fsync_dir(runtime.parent)
                except OSError:
                    pass
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
