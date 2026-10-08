from multiprocessing import get_context
from pathlib import Path

import pytest


def _try_account_lease(runtime_dir: str, identity: str, result_queue) -> None:
    from afuture.runtime_lease import AccountExclusiveRuntimeLease, RuntimeLeaseError

    lease = AccountExclusiveRuntimeLease(runtime_dir, identity, role="live")
    try:
        lease.acquire()
    except RuntimeLeaseError:
        result_queue.put("blocked")
        return
    try:
        result_queue.put("acquired")
    finally:
        lease.release()


def test_account_lease_survives_lock_file_unlink_across_processes(tmp_path: Path):
    from afuture.runtime_lease import AccountExclusiveRuntimeLease

    identity = "e" * 64
    first = AccountExclusiveRuntimeLease(tmp_path / "a", identity, role="live")
    first.acquire()
    context = get_context("spawn")
    result_queue = context.Queue()
    try:
        for path in first.paths:
            path.unlink()
        contender = context.Process(
            target=_try_account_lease,
            args=(str(tmp_path / "b"), identity, result_queue),
        )
        contender.start()
        contender.join(timeout=10)
        assert contender.exitcode == 0
        assert result_queue.get(timeout=2) == "blocked"
    finally:
        first.release()


def test_runtime_lease_cannot_be_bypassed_with_a_different_account_identity(tmp_path: Path):
    from afuture.runtime_lease import AccountExclusiveRuntimeLease, RuntimeLeaseError

    runtime_dir = tmp_path / "shared-runtime"
    first = AccountExclusiveRuntimeLease(runtime_dir, "f" * 64, role="live")
    wrong_identity = AccountExclusiveRuntimeLease(runtime_dir, "0" * 64, role="live")
    first.acquire()
    try:
        for path in first.paths:
            path.unlink()

        with pytest.raises(RuntimeLeaseError, match="already owned"):
            wrong_identity.acquire()
    finally:
        first.release()
