"""Stable Stress-90 bootstrap bundle API with explicit test-only fixtures."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any

from .directional_stress90_bootstrap import (
    OFFICIAL_STRESS90_BOOTSTRAP_EXPECTATIONS,
    Stress90BootstrapExpectations,
)
from .directional_stress90_state import Stress90SeedStore
from .provenance import ProvenanceError, git_head, repository_root
from .secure_archive import (
    SecureArchiveError,
    build_deterministic_archive,
    canonical_json_bytes,
    verify_archive,
)
from .stress90_bootstrap_bundle import (
    BootstrapBundleError,
    create_stress90_bundle as _create_production_bundle,
    install_stress90_bundle as _install_production_bundle,
    verify_stress90_bundle as _verify_production_bundle,
)

Stress90BundleError = BootstrapBundleError

_BUNDLE_KIND = "afuture.stress90.bootstrap-bundle"
_SCHEMA_VERSION = 1
_TEST_PROFILE = "test-only"
_TEST_MEMBERS = ("oi.json", "policy.json", "seed.json")
_HEX40 = re.compile(r"[0-9a-f]{40}")


def _official(expectations: Stress90BootstrapExpectations) -> bool:
    official = OFFICIAL_STRESS90_BOOTSTRAP_EXPECTATIONS
    return bool(
        expectations.official_historical_profile is True
        and dict(expectations.input_sha256) == dict(official.input_sha256)
        and expectations.candidate_weight_sha256 == official.candidate_weight_sha256
    )


def create_stress90_bundle(
    *,
    source_runtime: str | Path,
    through_day: str,
    output_path: str | Path,
    source_commit: str,
    expectations: Stress90BootstrapExpectations = OFFICIAL_STRESS90_BOOTSTRAP_EXPECTATIONS,
) -> dict[str, object]:
    """Create a production bundle only from the official immutable bootstrap profile."""

    if not _official(expectations):
        raise Stress90BundleError("production bundle creation requires the official profile")
    if not isinstance(source_commit, str) or _HEX40.fullmatch(source_commit) is None:
        raise Stress90BundleError("production bundle source commit must be full 40-hex")
    try:
        repo = repository_root()
        if git_head(repo) != source_commit:
            raise Stress90BundleError("production bundle source commit does not match current HEAD")
        seed = Stress90SeedStore(Path(source_runtime) / "stress90_bootstrap_seed.json").load_required()
        if seed.bootstrap_through_day != through_day:
            raise Stress90BundleError("production bundle through day differs from clean seed")
        result = _create_production_bundle(
            runtime_dir=source_runtime,
            output_path=output_path,
            repo_dir=repo,
        )
        return {
            "verified": True,
            "production_ready": True,
            "profile": "official",
            "bundle_digest": result.bundle_digest,
            "archive_sha256": result.archive_sha256,
        }
    except Stress90BundleError:
        raise
    except (OSError, ProvenanceError, RuntimeError, ValueError) as exc:
        raise Stress90BundleError(str(exc)) from exc


def _test_fixture_manifest(payload_members: dict[str, bytes]) -> dict[str, Any]:
    member_meta = {
        name: {"sha256": sha256(payload).hexdigest(), "size": len(payload)}
        for name, payload in sorted(payload_members.items())
    }
    unsigned: dict[str, Any] = {
        "kind": _BUNDLE_KIND,
        "schema_version": _SCHEMA_VERSION,
        "profile": _TEST_PROFILE,
        "production_ready": False,
        "members": member_meta,
    }
    return {
        **unsigned,
        "bundle_digest": sha256(canonical_json_bytes(unsigned)).hexdigest(),
    }


def build_test_fixture_bundle(path: str | Path) -> dict[str, object]:
    """Create a clearly test-only bundle that can never pass production verification."""

    payload_members = {
        "seed.json": canonical_json_bytes({"kind": "afuture.test.seed", "account_bound": False}),
        "policy.json": canonical_json_bytes(
            {"kind": "afuture.test.policy", "account_bound": False}
        ),
        "oi.json": canonical_json_bytes({"kind": "afuture.test.oi", "source": "fixture"}),
    }
    manifest = _test_fixture_manifest(payload_members)
    members = {"manifest.json": canonical_json_bytes(manifest), **payload_members}
    try:
        archive_sha = build_deterministic_archive(path, members)
    except SecureArchiveError as exc:
        raise Stress90BundleError(str(exc)) from exc
    return {
        "verified": True,
        "production_ready": False,
        "profile": _TEST_PROFILE,
        "bundle_digest": manifest["bundle_digest"],
        "archive_sha256": archive_sha,
    }


def _decode_fixture_manifest(payload: bytes) -> dict[str, Any]:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stress90BundleError("bundle manifest is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise Stress90BundleError("bundle manifest must be a JSON object")
    return raw


def _verify_test_fixture(path: str | Path) -> dict[str, object]:
    allowed = {"manifest.json", *_TEST_MEMBERS}
    try:
        archive = verify_archive(
            path,
            allowed_members=allowed,
            required_members=allowed,
        )
    except SecureArchiveError as exc:
        raise Stress90BundleError(str(exc)) from exc
    members = dict(archive.members)
    manifest = _decode_fixture_manifest(members["manifest.json"])
    fields = {
        "bundle_digest",
        "kind",
        "members",
        "production_ready",
        "profile",
        "schema_version",
    }
    if (
        set(manifest) != fields
        or manifest.get("kind") != _BUNDLE_KIND
        or manifest.get("schema_version") != _SCHEMA_VERSION
        or manifest.get("profile") != _TEST_PROFILE
        or manifest.get("production_ready") is not False
    ):
        raise Stress90BundleError("test-only bundle manifest identity is invalid")
    member_meta = manifest.get("members")
    if not isinstance(member_meta, dict) or set(member_meta) != set(_TEST_MEMBERS):
        raise Stress90BundleError("test-only bundle member manifest is invalid")
    for name in _TEST_MEMBERS:
        meta = member_meta[name]
        payload = members[name]
        if (
            not isinstance(meta, dict)
            or set(meta) != {"sha256", "size"}
            or meta["size"] != len(payload)
            or meta["sha256"] != sha256(payload).hexdigest()
        ):
            raise Stress90BundleError(f"test-only bundle member digest mismatch: {name}")
    unsigned = {key: value for key, value in manifest.items() if key != "bundle_digest"}
    expected = sha256(canonical_json_bytes(unsigned)).hexdigest()
    if manifest.get("bundle_digest") != expected:
        raise Stress90BundleError("test-only bundle digest mismatch")
    if members["manifest.json"] != canonical_json_bytes(manifest):
        raise Stress90BundleError("test-only bundle manifest is not canonical JSON")
    return {
        "verified": True,
        "production_ready": False,
        "profile": _TEST_PROFILE,
        "bundle_digest": expected,
        "archive_sha256": archive.archive_sha256,
    }


def verify_stress90_bundle(
    path: str | Path,
    *,
    allow_test_fixture: bool = False,
) -> dict[str, object]:
    """Verify a production bundle or, only when explicit, a test-only fixture bundle."""

    try:
        probe = verify_archive(path)
    except SecureArchiveError as exc:
        raise Stress90BundleError(str(exc)) from exc
    manifest_payload = probe.members.get("manifest.json")
    if manifest_payload is None:
        raise Stress90BundleError("bundle manifest is missing")
    manifest = _decode_fixture_manifest(manifest_payload)
    if manifest.get("profile") == _TEST_PROFILE:
        if not allow_test_fixture:
            raise Stress90BundleError("test-only bundle is never production-ready")
        return _verify_test_fixture(path)
    result = _verify_production_bundle(path, require_production=True)
    return {
        "verified": True,
        "production_ready": result.production_ready,
        "profile": "official",
        "bundle_digest": result.bundle_digest,
        "archive_sha256": result.archive_sha256,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_staging_directory(staging: Path, runtime: Path) -> None:
    if runtime.exists():
        if not runtime.is_dir() or runtime.is_symlink() or any(runtime.iterdir()):
            raise Stress90BundleError("bundle install target must be empty")
        runtime.rmdir()
        _fsync_directory(runtime.parent)
    os.rename(staging, runtime)
    _fsync_directory(runtime.parent)


def _install_test_fixture(bundle: Path, runtime: Path) -> dict[str, object]:
    report = _verify_test_fixture(bundle)
    if runtime.exists() and (
        not runtime.is_dir() or runtime.is_symlink() or any(runtime.iterdir())
    ):
        raise Stress90BundleError("bundle install target must be empty")
    runtime.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{runtime.name}.install-", dir=runtime.parent))
    published = False
    try:
        archive = verify_archive(
            bundle,
            allowed_members={"manifest.json", *_TEST_MEMBERS},
            required_members={"manifest.json", *_TEST_MEMBERS},
        )
        for name in _TEST_MEMBERS:
            target = staging / name
            descriptor = os.open(
                target,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.write(descriptor, archive.members[name])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _fsync_directory(staging)
        _publish_staging_directory(staging, runtime)
        published = True
        return {
            **report,
            "installed": True,
            "runtime_dir": str(runtime.resolve(strict=False)),
            "account_bound": False,
            "activation_permit_created": False,
            "orders_sent": 0,
            "cancels_sent": 0,
        }
    except Exception as exc:
        if isinstance(exc, Stress90BundleError):
            raise
        raise Stress90BundleError(f"bundle install failed: {exc}") from exc
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def install_stress90_bundle(
    bundle_path: str | Path,
    runtime_dir: str | Path,
    *,
    allow_test_fixture: bool = False,
) -> dict[str, object]:
    """Install only into an empty runtime; test fixtures require explicit opt-in."""

    bundle = Path(bundle_path)
    runtime = Path(runtime_dir).resolve(strict=False)
    report = verify_stress90_bundle(bundle, allow_test_fixture=allow_test_fixture)
    if report["profile"] == _TEST_PROFILE:
        return _install_test_fixture(bundle, runtime)
    try:
        return _install_production_bundle(
            bundle_path=bundle,
            runtime_dir=runtime,
        )
    except Stress90BundleError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise Stress90BundleError(f"bundle install failed: {exc}") from exc
