from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_native_module_identity_binds_every_native_extension(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.provenance as provenance

    package = tmp_path / "vnpy_ctp"
    package.mkdir()
    market = package / "vnctpmd.so"
    trading = package / "vnctptd.so"
    market.write_bytes(b"market-v1")
    trading.write_bytes(b"trading-v1")

    spec = SimpleNamespace(submodule_search_locations=[str(package)], origin=None)
    monkeypatch.setattr(provenance.importlib.util, "find_spec", lambda _package: spec)

    first = provenance.native_module_identity("vnpy_ctp")
    assert first is not None
    assert len(first["files"]) == 2
    assert {Path(item["path"]).name for item in first["files"]} == {
        "vnctpmd.so",
        "vnctptd.so",
    }

    trading.write_bytes(b"trading-v2")
    second = provenance.native_module_identity("vnpy_ctp")
    assert second is not None
    assert second["sha256"] != first["sha256"]


def test_verified_frozen_inputs_return_verified_bytes_and_basename_sorted_manifest(
    tmp_path: Path,
) -> None:
    from afuture.provenance import verify_frozen_input_files

    contents = {"z.csv": b"z-data", "a.csv": b"a-data"}
    for name, payload in contents.items():
        (tmp_path / name).write_bytes(payload)
    expected = {name: hashlib.sha256(payload).hexdigest() for name, payload in contents.items()}

    verified, manifest = verify_frozen_input_files(tmp_path, expected)

    assert verified == contents
    assert manifest == [
        {
            "basename": "a.csv",
            "sha256": hashlib.sha256(b"a-data").hexdigest(),
            "size_bytes": 6,
        },
        {
            "basename": "z.csv",
            "sha256": hashlib.sha256(b"z-data").hexdigest(),
            "size_bytes": 6,
        },
    ]


def test_verified_frozen_inputs_reject_bad_digest_with_basename_expected_and_actual(
    tmp_path: Path,
) -> None:
    from afuture.provenance import ProvenanceError, verify_frozen_input_files

    path = tmp_path / "fixed.csv"
    path.write_bytes(b"actual")
    expected = "0" * 64

    with pytest.raises(
        ProvenanceError,
        match=rf"fixed\.csv.*expected={expected}.*actual={hashlib.sha256(b'actual').hexdigest()}",
    ):
        verify_frozen_input_files(tmp_path, {"fixed.csv": expected})


def test_verified_frozen_inputs_reject_symlink_before_returning_any_bytes(tmp_path: Path) -> None:
    from afuture.provenance import ProvenanceError, verify_frozen_input_files

    target = tmp_path / "target.csv"
    target.write_bytes(b"data")
    alias = tmp_path / "fixed.csv"
    alias.symlink_to(target)

    with pytest.raises(ProvenanceError, match="regular file safely|identity or size"):
        verify_frozen_input_files(
            tmp_path,
            {"fixed.csv": hashlib.sha256(b"data").hexdigest()},
        )
