from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


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
