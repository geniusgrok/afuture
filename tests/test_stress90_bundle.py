from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest


def _unsafe_archive(*, name: str, data: bytes = b"x", kind: str = "file") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo(name)
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mode = 0o600
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "target"
            archive.addfile(info)
        elif kind == "hardlink":
            info.type = tarfile.LNKTYPE
            info.linkname = "target"
            archive.addfile(info)
        else:
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def test_deterministic_archive_bytes_are_identical() -> None:
    from afuture.secure_archive import create_deterministic_archive

    members = {
        "manifest.json": b'{"a":1}',
        "seed.json": b"seed",
        "policy.json": b"policy",
    }
    assert create_deterministic_archive(members) == create_deterministic_archive(members)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("../escape", "file"),
        ("/absolute", "file"),
        ("seed.json", "symlink"),
        ("seed.json", "hardlink"),
    ],
)
def test_archive_rejects_path_and_link_attacks(name: str, kind: str) -> None:
    from afuture.secure_archive import ArchiveSecurityError, read_deterministic_archive

    payload = _unsafe_archive(name=name, kind=kind)
    with pytest.raises(ArchiveSecurityError):
        read_deterministic_archive(payload, allowed_members={"manifest.json", "seed.json"})


def test_archive_rejects_duplicate_member() -> None:
    from afuture.secure_archive import ArchiveSecurityError, read_deterministic_archive

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for data in (b"first", b"second"):
            info = tarfile.TarInfo("seed.json")
            info.size = len(data)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    with pytest.raises(ArchiveSecurityError, match="duplicate"):
        read_deterministic_archive(stream.getvalue(), allowed_members={"seed.json"})


def test_archive_rejects_unknown_member() -> None:
    from afuture.secure_archive import ArchiveSecurityError, read_deterministic_archive

    payload = _unsafe_archive(name="unknown.bin")
    with pytest.raises(ArchiveSecurityError, match="unknown"):
        read_deterministic_archive(payload, allowed_members={"manifest.json"})


def test_archive_rejects_member_and_total_size_limits() -> None:
    from afuture.secure_archive import (
        ArchiveSecurityError,
        create_deterministic_archive,
        read_deterministic_archive,
    )

    payload = create_deterministic_archive({"a": b"1234", "b": b"5678"})
    with pytest.raises(ArchiveSecurityError, match="member.*size"):
        read_deterministic_archive(
            payload,
            allowed_members={"a", "b"},
            max_member_bytes=3,
            max_total_bytes=100,
        )
    with pytest.raises(ArchiveSecurityError, match="total.*size"):
        read_deterministic_archive(
            payload,
            allowed_members={"a", "b"},
            max_member_bytes=10,
            max_total_bytes=7,
        )


def test_archive_rejects_truncation_append_and_tamper() -> None:
    from afuture.secure_archive import (
        ArchiveSecurityError,
        create_deterministic_archive,
        read_deterministic_archive,
    )

    payload = create_deterministic_archive(
        {"manifest.json": b'{"a":1}', "seed.json": b"seed"}
    )
    variants = [
        payload[:-512],
        payload + b"garbage",
        payload[:1024] + bytes([payload[1024] ^ 1]) + payload[1025:],
    ]
    for variant in variants:
        with pytest.raises(ArchiveSecurityError):
            read_deterministic_archive(
                variant,
                allowed_members={"manifest.json", "seed.json"},
            )


def test_bundle_rejects_non_official_expectations(tmp_path: Path) -> None:
    from afuture.stress90_bundle import Stress90BundleError, create_stress90_bundle

    from afuture.directional_stress90_bootstrap import Stress90BootstrapExpectations

    expectations = Stress90BootstrapExpectations(
        input_sha256={
            "broad_daily_universe.csv": "1" * 64,
            "return_target_specific_contracts.csv": "2" * 64,
            "execution_aligned_weights.csv": "3" * 64,
            "prior_two_year_broad_60m.csv": "4" * 64,
            "two_year_broad_60m.csv": "5" * 64,
        },
        candidate_weight_sha256="6" * 64,
        official_historical_profile=False,
    )
    with pytest.raises(Stress90BundleError, match="official"):
        create_stress90_bundle(
            source_runtime=tmp_path / "runtime",
            through_day="20260824",
            output_path=tmp_path / "bundle.tar",
            source_commit="a" * 40,
            expectations=expectations,
        )


def test_test_fixture_bundle_is_never_production_ready(tmp_path: Path) -> None:
    from afuture.stress90_bundle import build_test_fixture_bundle, verify_stress90_bundle

    path = tmp_path / "fixture.tar"
    build_test_fixture_bundle(path)
    report = verify_stress90_bundle(path, allow_test_fixture=True)
    assert report["verified"] is True
    assert report["production_ready"] is False
    assert report["profile"] == "test-only"


def test_production_verify_rejects_test_fixture_bundle(tmp_path: Path) -> None:
    from afuture.stress90_bundle import (
        Stress90BundleError,
        build_test_fixture_bundle,
        verify_stress90_bundle,
    )

    path = tmp_path / "fixture.tar"
    build_test_fixture_bundle(path)
    with pytest.raises(Stress90BundleError, match="test-only"):
        verify_stress90_bundle(path)


def test_bundle_install_rejects_nonempty_runtime(tmp_path: Path) -> None:
    from afuture.stress90_bundle import (
        Stress90BundleError,
        build_test_fixture_bundle,
        install_stress90_bundle,
    )

    bundle = tmp_path / "fixture.tar"
    build_test_fixture_bundle(bundle)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "existing").write_text("authority", encoding="utf-8")
    with pytest.raises(Stress90BundleError, match="empty"):
        install_stress90_bundle(bundle, runtime, allow_test_fixture=True)


def test_bundle_install_failure_does_not_publish_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.stress90_bundle as bundle_module

    bundle = tmp_path / "fixture.tar"
    bundle_module.build_test_fixture_bundle(bundle)
    runtime = tmp_path / "runtime"

    def fail_publish(_staging: Path, _runtime: Path) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(bundle_module, "_publish_staging_directory", fail_publish)
    with pytest.raises(bundle_module.Stress90BundleError, match="install"):
        bundle_module.install_stress90_bundle(bundle, runtime, allow_test_fixture=True)
    assert not runtime.exists()
