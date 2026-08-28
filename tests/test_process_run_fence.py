from __future__ import annotations

from pathlib import Path

import pytest

from afuture import stress90_activation_permit as permit_module
from afuture.models import RuntimeMode
from afuture.process_run import (
    PROCESS_FENCE_EXIT_CODE,
    ProcessRunIntegrityError,
    ProcessRunStore,
    apply_unclean_restart_fence,
)
from afuture.state import RuntimeState, StateStore

IDENTITY = {
    "deployment_digest": "a" * 64,
    "runtime_identity_digest": "b" * 64,
    "account_identity_digest": "c" * 64,
    "start_state_checksum": "d" * 64,
}


def test_clean_process_run_restart_chains_current_and_prev(tmp_path: Path) -> None:
    store = ProcessRunStore(tmp_path / "process_run.json")
    first = store.begin(**IDENTITY, process_uuid="11111111-1111-4111-8111-111111111111")
    closed = store.finish_clean(
        first.process_uuid,
        stop_state_checksum="e" * 64,
        stopped_utc="2026-08-28T00:01:00+00:00",
    )
    assert closed.clean_shutdown is True
    second = store.begin(**IDENTITY, process_uuid="22222222-2222-4222-8222-222222222222")
    assert second.sequence == closed.sequence + 1
    assert second.parent_checksum == closed.checksum
    assert store.previous_path.exists()


@pytest.mark.parametrize(
    "phase",
    [
        "run_marker_written",
        "broker_constructed",
        "broker_ready_not_activated",
        "permit_consumed",
        "running_state_pending",
        "running",
        "shutdown_state_saved",
        "heartbeat_stopped_pending_receipt",
    ],
)
def test_every_uncertain_crash_phase_is_not_clean(tmp_path: Path, phase: str) -> None:
    store = ProcessRunStore(tmp_path / f"{phase}.json")
    record = store.begin(**IDENTITY)
    updated = store.mark_phase(record.process_uuid, phase, state_checksum="f" * 64)
    assert updated.clean_shutdown is False
    assert updated.phase == phase


def test_unclean_restart_fence_halts_state_invalidates_permit_and_exits_special_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_store = ProcessRunStore(tmp_path / "process_run.json")
    process_store.begin(**IDENTITY)
    state_store = StateStore(tmp_path / "state.json")
    initial = RuntimeState(
        kill_switch=False,
        runtime_mode=RuntimeMode.RUNNING.value,
        reconciled=True,
        metadata_verified=True,
    )
    state_store.save(initial)
    invalidations: list[str] = []

    monkeypatch.setattr(
        permit_module.Stress90ActivationPermitStore,
        "invalidate",
        lambda _self, reason: invalidations.append(reason),
    )
    (tmp_path / "stress90_activation_permit.json").write_text("placeholder", encoding="utf-8")

    result = apply_unclean_restart_fence(
        process_store=process_store,
        state_store=state_store,
        runtime_dir=tmp_path,
        deployment_digest="a" * 64,
        runtime_identity_digest="b" * 64,
        account_identity_digest="c" * 64,
    )
    assert result.blocked is True
    assert result.exit_code == PROCESS_FENCE_EXIT_CODE
    fenced = state_store.load_required_record()
    assert fenced.state.kill_switch is True
    assert fenced.state.runtime_mode == RuntimeMode.HALTED.value
    assert fenced.state.reconciled is False
    assert "unclean restart" in fenced.state.kill_reason
    assert invalidations and "unclean restart" in invalidations[0]
    receipt = process_store.load_required()
    assert receipt.clean_shutdown is True
    assert receipt.phase == "restart_fenced"


def test_restart_fence_uses_exact_state_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_store = ProcessRunStore(tmp_path / "process_run.json")
    process_store.begin(**IDENTITY)
    state_store = StateStore(tmp_path / "state.json")
    original = state_store.save(RuntimeState())
    calls: list[tuple[int | None, str | None]] = []
    real_save = state_store.save

    def tracked_save(state, *, expected_sequence=None, expected_checksum=None):
        calls.append((expected_sequence, expected_checksum))
        return real_save(
            state,
            expected_sequence=expected_sequence,
            expected_checksum=expected_checksum,
        )

    monkeypatch.setattr(state_store, "save", tracked_save)
    apply_unclean_restart_fence(
        process_store=process_store,
        state_store=state_store,
        runtime_dir=tmp_path,
        deployment_digest="a" * 64,
        runtime_identity_digest="b" * 64,
        account_identity_digest="c" * 64,
    )
    assert calls[0] == (original.sequence, original.checksum)


def test_process_run_current_corruption_never_falls_back_to_prev(tmp_path: Path) -> None:
    store = ProcessRunStore(tmp_path / "process_run.json")
    first = store.begin(**IDENTITY)
    store.finish_clean(first.process_uuid, stop_state_checksum="e" * 64)
    second = store.begin(**IDENTITY)
    store.finish_clean(second.process_uuid, stop_state_checksum="f" * 64)
    store.path.write_bytes(b"corrupt")
    with pytest.raises(ProcessRunIntegrityError):
        store.load_required()
    assert store.previous_path.exists()


def test_process_run_missing_current_with_prev_is_incident(tmp_path: Path) -> None:
    store = ProcessRunStore(tmp_path / "process_run.json")
    first = store.begin(**IDENTITY)
    store.finish_clean(first.process_uuid, stop_state_checksum="e" * 64)
    second = store.begin(**IDENTITY)
    store.finish_clean(second.process_uuid, stop_state_checksum="f" * 64)
    store.path.unlink()
    with pytest.raises(ProcessRunIntegrityError, match="current is missing"):
        store.load_required()


def test_process_run_rejects_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("x", encoding="utf-8")
    path = tmp_path / "process_run.json"
    path.symlink_to(victim)
    store = ProcessRunStore(path)
    with pytest.raises(ProcessRunIntegrityError, match="symlink"):
        store.begin(**IDENTITY)
    assert victim.read_text(encoding="utf-8") == "x"
