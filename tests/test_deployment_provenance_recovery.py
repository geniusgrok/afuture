from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from afuture.deployment_identity import DeploymentIdentityError, DeploymentIdentityStore
from afuture.runtime_backup import RuntimeBackupError, verify_backup_archive
from afuture.secure_archive import SecureArchiveError, build_deterministic_archive, verify_archive
from afuture.stress90_bootstrap_bundle import BootstrapBundleError, verify_stress90_bundle


def test_secure_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as handle:
        info = tarfile.TarInfo("../escape")
        payload = b"x"
        info.size = len(payload)
        handle.addfile(info, io.BytesIO(payload))
    with pytest.raises(SecureArchiveError):
        verify_archive(archive, allowed_members={"manifest.json"})


def test_deterministic_archive_bytes_repeat(tmp_path: Path) -> None:
    members = {"manifest.json": b"{}", "state.json": b"payload"}
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"
    build_deterministic_archive(first, members)
    build_deterministic_archive(second, members)
    assert first.read_bytes() == second.read_bytes()


def test_deployment_store_never_falls_back_to_prev(tmp_path: Path) -> None:
    path = tmp_path / "deployment_identity.json"
    store = DeploymentIdentityStore(path)
    first = store.seal({"source_commit": "a" * 40})
    store.seal({"source_commit": "b" * 40})
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(DeploymentIdentityError):
        store.load_required()
    assert store.previous_path.exists()
    assert first.checksum in store.previous_path.read_text(encoding="utf-8")


def test_bundle_verify_rejects_unknown_member(tmp_path: Path) -> None:
    manifest = {
        "kind": "afuture.stress90.bootstrap-bundle",
        "schema_version": 1,
        "production_ready": False,
        "members": {"seed.json": {"sha256": "0" * 64, "size": 0}},
        "bundle_digest": "0" * 64,
    }
    archive = tmp_path / "bundle.tar"
    build_deterministic_archive(
        archive,
        {
            "manifest.json": json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
            "seed.json": b"",
            "surprise.txt": b"x",
        },
    )
    with pytest.raises(BootstrapBundleError):
        verify_stress90_bundle(archive, require_production=False)


def test_backup_verify_rejects_non_backup_archive(tmp_path: Path) -> None:
    archive = tmp_path / "not-backup.tar"
    build_deterministic_archive(archive, {"manifest.json": b"{}"})
    with pytest.raises(RuntimeBackupError):
        verify_backup_archive(archive)
