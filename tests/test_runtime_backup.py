from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.models import RuntimeMode
from afuture.runtime_backup import BackupVerification, RuntimeBackupError
from afuture.state import RuntimeState, StateStore


def _restore_manifest(runtime: Path, registry: Path) -> dict[str, object]:
    return {
        "source_runtime_path": str(runtime.resolve(strict=False)),
        "source_account_registry_path": str(registry.resolve(strict=False)),
        "state_filename": "directional_state.json",
        "runtime_mode": RuntimeMode.HALTED.value,
        "kill_switch": True,
        "account_identity_digest": "a" * 64,
        "account_epoch": "b" * 64,
    }


def test_backup_allowlist_preserves_authoritative_pending_witnesses() -> None:
    import afuture.runtime_backup_impl as backup_module

    assert backup_module._allowed_member(
        "runtime/directional_ohlc_cache.json.pending",
        "directional_state.json",
    )
    assert backup_module._allowed_member(
        "runtime/stress90_oi_evidence.json.lineage",
        "directional_state.json",
    )
    assert backup_module._allowed_member(
        "registry/nonce-ledger/pending.json",
        "directional_state.json",
    )
    assert not backup_module._allowed_member(
        "runtime/afuture.log",
        "directional_state.json",
    )
    assert not backup_module._allowed_member(
        "runtime/../escape",
        "directional_state.json",
    )


@pytest.mark.parametrize(
    ("runtime_mode", "kill_switch", "message"),
    [
        (RuntimeMode.RUNNING.value, True, "HALTED"),
        (RuntimeMode.HALTED.value, False, "kill switch"),
    ],
)
def test_backup_rejects_running_or_unprotected_runtime_before_other_authority_reads(
    tmp_path: Path,
    runtime_mode: str,
    kill_switch: bool,
    message: str,
) -> None:
    import afuture.runtime_backup_impl as backup_module

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state_path = runtime / "directional_state.json"
    StateStore(state_path).save(
        RuntimeState(runtime_mode=runtime_mode, kill_switch=kill_switch)
    )
    with pytest.raises(RuntimeBackupError, match=message):
        backup_module._authority(
            runtime=runtime,
            state_path=state_path,
            registry_path=tmp_path / "registry.json",
            bound_runtime=runtime,
            bound_registry_path=tmp_path / "registry.json",
        )


def test_backup_rejects_prepared_lifecycle_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.runtime_backup_impl as backup_module

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state_path = runtime / "directional_state.json"
    StateStore(state_path).save(
        RuntimeState(runtime_mode=RuntimeMode.HALTED.value, kill_switch=True)
    )
    seed = SimpleNamespace(seed_digest="s")
    policy = SimpleNamespace(
        bootstrap_seed_digest="s",
        live_account_identity_digest="a" * 64,
        live_account_epoch="b" * 64,
    )
    policy_record = SimpleNamespace(state=policy)
    monkeypatch.setattr(
        backup_module,
        "Stress90SeedStore",
        lambda _path: SimpleNamespace(load_required=lambda: seed),
    )
    monkeypatch.setattr(
        backup_module,
        "Stress90PolicyStateStore",
        lambda _path: SimpleNamespace(load_required_record=lambda: policy_record),
    )
    monkeypatch.setattr(
        backup_module,
        "Stress90LifecycleTransactionStore",
        lambda _path: SimpleNamespace(load=lambda: SimpleNamespace(status="prepared")),
    )
    with pytest.raises(RuntimeBackupError, match="prepared lifecycle"):
        backup_module._authority(
            runtime=runtime,
            state_path=state_path,
            registry_path=tmp_path / "registry.json",
            bound_runtime=runtime,
            bound_registry_path=tmp_path / "registry.json",
        )


def test_restore_rejects_nonempty_runtime_and_preserves_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.runtime_backup_impl as backup_module

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "existing").write_text("authority", encoding="utf-8")
    registry = tmp_path / "account-runtime-registry.json"
    backup = tmp_path / "backup.tar"
    backup.write_bytes(b"immutable-backup")
    manifest = _restore_manifest(runtime, registry)
    monkeypatch.setattr(
        backup_module,
        "verify_backup_archive",
        lambda _path: BackupVerification(manifest=manifest, archive_sha256="c" * 64),
    )
    with pytest.raises(RuntimeBackupError, match="must be empty"):
        backup_module.restore_runtime(
            backup_path=backup,
            runtime_dir=runtime,
            account_registry_path=registry,
            registry_staging_path=tmp_path / "registry-stage",
        )
    assert backup.read_bytes() == b"immutable-backup"


def test_restore_refuses_runtime_rebase_or_registry_retarget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.runtime_backup_impl as backup_module

    source_runtime = tmp_path / "source-runtime"
    source_registry = tmp_path / "source-registry.json"
    manifest = _restore_manifest(source_runtime, source_registry)
    monkeypatch.setattr(
        backup_module,
        "verify_backup_archive",
        lambda _path: BackupVerification(manifest=manifest, archive_sha256="c" * 64),
    )
    with pytest.raises(RuntimeBackupError, match="runtime differs"):
        backup_module.restore_runtime(
            backup_path=tmp_path / "backup.tar",
            runtime_dir=tmp_path / "other-runtime",
            account_registry_path=source_registry,
            registry_staging_path=tmp_path / "registry-stage",
        )


def test_restore_success_remains_halted_invalidates_permit_and_never_constructs_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.broker.ctp as ctp_module
    import afuture.runtime_backup_impl as backup_module

    runtime = tmp_path / "runtime"
    registry = tmp_path / "account-runtime-registry.json"
    manifest = _restore_manifest(runtime, registry)
    backup = tmp_path / "backup.tar"
    backup.write_bytes(b"backup")
    monkeypatch.setattr(
        backup_module,
        "verify_backup_archive",
        lambda _path: BackupVerification(manifest=manifest, archive_sha256="c" * 64),
    )
    monkeypatch.setattr(
        backup_module,
        "verify_archive",
        lambda _path, **_kwargs: SimpleNamespace(members={"manifest.json": b"{}"}),
    )
    monkeypatch.setattr(
        backup_module,
        "_semantic_verify",
        lambda **_kwargs: {"runtime_mode": RuntimeMode.HALTED.value, "kill_switch": True},
    )
    monkeypatch.setattr(backup_module, "_publish_registry", lambda _source, _target: None)

    state = SimpleNamespace(runtime_mode=RuntimeMode.HALTED.value, kill_switch=True)
    state_record = SimpleNamespace(state=state)

    class FakeStateStore:
        def __init__(self, _path: Path) -> None:
            pass

        def load_required_record(self):
            return state_record

    policy = SimpleNamespace(
        live_account_identity_digest="a" * 64,
        live_account_epoch="b" * 64,
    )

    class FakePolicyStore:
        def __init__(self, _path: Path) -> None:
            pass

        def load_required(self):
            return policy

    class FakeRegistry:
        def __init__(self, _path: Path) -> None:
            pass

        def require_binding_evidence(self, *_args):
            return SimpleNamespace()

    permit_status = {"value": "issued"}

    class FakePermitStore:
        def __init__(self, _path: Path) -> None:
            pass

        def load_record(self):
            return SimpleNamespace(permit=SimpleNamespace(status=permit_status["value"]))

        def invalidate(self, _reason: str):
            permit_status["value"] = "invalidated"
            return None

    monkeypatch.setattr(backup_module, "StateStore", FakeStateStore)
    monkeypatch.setattr(backup_module, "Stress90PolicyStateStore", FakePolicyStore)
    monkeypatch.setattr(backup_module, "AccountRuntimeRegistry", FakeRegistry)
    monkeypatch.setattr(backup_module, "Stress90ActivationPermitStore", FakePermitStore)
    monkeypatch.setattr(
        ctp_module,
        "CtpBroker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Broker constructed")),
    )

    result = backup_module.restore_runtime(
        backup_path=backup,
        runtime_dir=runtime,
        account_registry_path=registry,
        registry_staging_path=tmp_path / "registry-stage",
    )
    assert result["runtime_mode"] == RuntimeMode.HALTED.value
    assert result["kill_switch"] is True
    assert result["activation_permit_valid"] is False
    assert result["orders_sent"] == 0
    assert result["cancels_sent"] == 0
    assert permit_status["value"] == "invalidated"
    assert result["next_steps"] == [
        "status",
        "deployment-verify",
        "doctor",
        "issue-fresh-permit",
    ]


def test_verify_backup_propagates_journal_or_cross_file_integrity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.runtime_backup_impl as backup_module

    manifest = _restore_manifest(tmp_path / "runtime", tmp_path / "account-runtime-registry.json")
    members = {"manifest.json": b"{}"}
    monkeypatch.setattr(
        backup_module,
        "verify_archive",
        lambda _path, **_kwargs: SimpleNamespace(
            members=members,
            archive_sha256="d" * 64,
        ),
    )
    monkeypatch.setattr(backup_module, "_decode_manifest", lambda _payload: manifest)
    monkeypatch.setattr(backup_module, "_validate_manifest", lambda _manifest, _members: "state.json")
    monkeypatch.setattr(
        backup_module,
        "_materialize",
        lambda **_kwargs: (tmp_path / "materialized-runtime", tmp_path / "materialized-registry"),
    )
    monkeypatch.setattr(
        backup_module,
        "_semantic_verify",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeBackupError("journal chain damaged")),
    )
    with pytest.raises(RuntimeBackupError, match="journal chain damaged"):
        backup_module.verify_backup_archive(tmp_path / "backup.tar")
