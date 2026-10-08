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
from afuture.runtime_lease import AccountExclusiveRuntimeLease
from afuture.state import RuntimeState, StateStore

IDENTITY = {
    "deployment_digest": "a" * 64,
    "runtime_identity_digest": "b" * 64,
    "account_identity_digest": "c" * 64,
    "start_state_checksum": "d" * 64,
}


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

    with AccountExclusiveRuntimeLease(tmp_path, "c" * 64, role="live") as lease:
        result = apply_unclean_restart_fence(
            process_store=process_store,
            state_store=state_store,
            runtime_dir=tmp_path,
            deployment_digest="a" * 64,
            runtime_identity_digest="b" * 64,
            account_identity_digest="c" * 64,
            lease=lease,
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
    with AccountExclusiveRuntimeLease(tmp_path, "c" * 64, role="live") as lease:
        apply_unclean_restart_fence(
            process_store=process_store,
            state_store=state_store,
            runtime_dir=tmp_path,
            deployment_digest="a" * 64,
            runtime_identity_digest="b" * 64,
            account_identity_digest="c" * 64,
            lease=lease,
        )
    assert calls[0] == (original.sequence, original.checksum)


@pytest.mark.parametrize("lease_kind", ["missing", "released", "foreign"])
def test_restart_fence_without_matching_held_lease_is_read_only(tmp_path, lease_kind):
    process_store = ProcessRunStore(tmp_path / "process_run.json")
    process_store.begin(**IDENTITY)
    state_store = StateStore(tmp_path / "state.json")
    initial = state_store.save(RuntimeState(kill_switch=False, runtime_mode="RUNNING"))
    before = process_store.path.read_bytes()
    lease = None
    if lease_kind != "missing":
        lease = AccountExclusiveRuntimeLease(
            tmp_path, ("e" if lease_kind == "foreign" else "c") * 64, role="live"
        )
        lease.acquire()
        if lease_kind == "released":
            lease.release()
    try:
        with pytest.raises(ProcessRunIntegrityError, match="held account/runtime lease"):
            apply_unclean_restart_fence(
                process_store=process_store,
                state_store=state_store,
                runtime_dir=tmp_path,
                deployment_digest="a" * 64,
                runtime_identity_digest="b" * 64,
                account_identity_digest="c" * 64,
                lease=lease,
            )
    finally:
        if lease is not None:
            lease.release()
    assert process_store.path.read_bytes() == before
    assert state_store.load_required_record().checksum == initial.checksum
