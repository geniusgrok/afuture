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


def test_account_exclusive_runtime_lease_rejects_second_process_owner(tmp_path: Path):
    from afuture.runtime_lease import AccountExclusiveRuntimeLease, RuntimeLeaseError

    first = AccountExclusiveRuntimeLease(tmp_path, "a" * 64, role="live")
    second = AccountExclusiveRuntimeLease(tmp_path, "a" * 64, role="live")
    first.acquire()
    try:
        with pytest.raises(RuntimeLeaseError, match="already owned"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_account_lease_exposes_exact_technical_activation_capability(tmp_path: Path):
    from afuture.runtime_lease import AccountExclusiveRuntimeLease

    identity = "a" * 64
    lease = AccountExclusiveRuntimeLease(tmp_path, identity, role="live")

    assert not lease.authorizes_technical_activation(identity, tmp_path)
    lease.acquire()
    try:
        assert lease.authorizes_technical_activation(identity, tmp_path)
        assert not lease.authorizes_technical_activation("b" * 64, tmp_path)
        assert not lease.authorizes_technical_activation(identity, tmp_path / "other")
    finally:
        lease.release()
    assert not lease.authorizes_technical_activation(identity, tmp_path)


def test_account_lease_is_shared_across_runtime_dirs_but_shadow_identity_isolated(tmp_path: Path):
    from afuture.runtime_lease import AccountExclusiveRuntimeLease, RuntimeLeaseError

    live = AccountExclusiveRuntimeLease(tmp_path / "a", "b" * 64, role="live")
    duplicate = AccountExclusiveRuntimeLease(tmp_path / "b", "b" * 64, role="live")
    shadow = AccountExclusiveRuntimeLease(tmp_path / "shadow", "c" * 64, role="shadow")
    live.acquire()
    try:
        with pytest.raises(RuntimeLeaseError, match="already owned"):
            duplicate.acquire()
        shadow.acquire()
        shadow.release()
    finally:
        live.release()


def test_account_lease_cannot_be_bypassed_by_unlinking_lock_files(tmp_path: Path):
    from afuture.runtime_lease import AccountExclusiveRuntimeLease, RuntimeLeaseError

    first = AccountExclusiveRuntimeLease(tmp_path / "a", "d" * 64, role="live")
    second = AccountExclusiveRuntimeLease(tmp_path / "b", "d" * 64, role="live")
    first.acquire()
    try:
        for path in first.paths:
            path.unlink()

        with pytest.raises(RuntimeLeaseError, match="already owned"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


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


def test_runtime_lease_symlink_alias_cannot_bypass_unlink_proof_runtime_identity(
    tmp_path: Path,
):
    from afuture.runtime_lease import AccountExclusiveRuntimeLease, RuntimeLeaseError

    real_runtime = tmp_path / "real-runtime"
    real_runtime.mkdir()
    alias_runtime = tmp_path / "alias-runtime"
    alias_runtime.symlink_to(real_runtime, target_is_directory=True)
    first = AccountExclusiveRuntimeLease(alias_runtime, "1" * 64, role="live")
    switched_account = AccountExclusiveRuntimeLease(
        real_runtime,
        "2" * 64,
        role="account-rebase",
    )

    first.acquire()
    try:
        for path in first.paths:
            path.unlink()

        with pytest.raises(RuntimeLeaseError, match="already owned"):
            switched_account.acquire()
    finally:
        first.release()


def test_runtime_lease_capability_uses_real_runtime_identity(tmp_path: Path):
    from afuture.runtime_lease import AccountExclusiveRuntimeLease

    real_runtime = tmp_path / "real-runtime"
    real_runtime.mkdir()
    alias_runtime = tmp_path / "alias-runtime"
    alias_runtime.symlink_to(real_runtime, target_is_directory=True)
    identity = "3" * 64
    lease = AccountExclusiveRuntimeLease(alias_runtime, identity, role="live")

    lease.acquire()
    try:
        assert lease.authorizes_technical_activation(identity, real_runtime)
    finally:
        lease.release()


def test_cli_help_imports_without_fcntl_and_acquire_fails_before_writes(tmp_path: Path):
    import subprocess
    import sys

    code = """
import sys
sys.modules['fcntl'] = None
from pathlib import Path
from afuture.runtime_lease import AccountExclusiveRuntimeLease, RuntimeLeaseError
from afuture import command_router
runtime = Path(sys.argv[1]) / 'must-not-be-created'
lease = AccountExclusiveRuntimeLease(runtime, '9' * 64, role='live')
try:
    lease.acquire()
except RuntimeLeaseError as exc:
    assert 'Linux OFD' in str(exc)
else:
    raise AssertionError('unsupported platform acquired lease')
assert not runtime.exists()
lease.release()
raise SystemExit(command_router.main(['--help']))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
