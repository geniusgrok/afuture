from __future__ import annotations

from pathlib import Path

import pytest


def test_restore_publication_quarantines_runtime_when_registry_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.runtime_backup as backup_module

    runtime = tmp_path / "runtime"
    runtime_stage = tmp_path / ".runtime.restore-stage"
    runtime_stage.mkdir()
    (runtime_stage / "marker").write_text("verified-halted-runtime", encoding="utf-8")

    registry = tmp_path / "machine" / "account-runtime-registry.json"
    registry.parent.mkdir()
    registry_stage_root = tmp_path / "registry-stage"
    registry_stage_root.mkdir()
    stage_registry = registry_stage_root / registry.name
    stage_registry.write_text("staged-registry", encoding="utf-8")

    def fail_registry_publish(_source: Path, target: Path) -> None:
        target.write_text("partial-registry-evidence", encoding="utf-8")
        raise OSError("simulated registry publication failure")

    monkeypatch.setattr(backup_module, "_publish_registry", fail_registry_publish)

    with pytest.raises(backup_module.RuntimeBackupError, match="runtime quarantined"):
        backup_module._publish_restore_transaction(
            runtime_stage=runtime_stage,
            runtime=runtime,
            stage_registry=stage_registry,
            registry=registry,
            registry_stage_root=registry_stage_root,
            incident_token="c" * 16,
        )

    assert not runtime.exists()
    incidents = list(tmp_path.glob(".runtime.failed-restore-cccccccccccccccc*"))
    assert len(incidents) == 1
    assert (incidents[0] / "marker").read_text(encoding="utf-8") == "verified-halted-runtime"
    assert registry.read_text(encoding="utf-8") == "partial-registry-evidence"
