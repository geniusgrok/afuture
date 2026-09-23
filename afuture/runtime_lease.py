"""Process-wide account/runtime execution lease for order-capable commands."""

from __future__ import annotations

import errno
import json
import os
import re
import struct
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import IO

_SHA256 = re.compile(r"[0-9a-f]{64}")


class RuntimeLeaseError(RuntimeError):
    """Another process already owns the account or runtime execution lease."""


def _locking_api():
    # Importing commands and help must work on research-only platforms.  The
    # unlink-proof account lease itself requires Linux OFD locking; never fall
    # back to weaker file-only locking on an unsupported platform.
    if sys.platform != "linux":
        raise RuntimeLeaseError("order-capable runtime requires Linux OFD locking")
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeLeaseError("order-capable runtime requires Linux OFD locking") from exc
    if not hasattr(fcntl, "F_OFD_SETLK"):
        raise RuntimeLeaseError("order-capable runtime requires Linux OFD locking")
    return fcntl


class AccountExclusiveRuntimeLease:
    """Hold both runtime-dir and account-global advisory locks until release."""

    def __init__(self, runtime_dir: str | Path, account_identity_digest: str, *, role: str) -> None:
        if _SHA256.fullmatch(account_identity_digest or "") is None:
            raise RuntimeLeaseError("account lease identity must be a lowercase SHA-256")
        if not isinstance(role, str) or not role.strip():
            raise RuntimeLeaseError("account lease role is required")
        runtime = Path(os.path.realpath(runtime_dir))
        global_path = Path(tempfile.gettempdir()) / (
            f"afuture-account-{account_identity_digest}.lock"
        )
        self.paths = tuple(sorted((runtime / ".account-exclusive.lock", global_path)))
        self.account_identity_digest = account_identity_digest
        self.runtime_identity_digest = sha256(f"runtime:{runtime}".encode()).hexdigest()
        self.role = role.strip()
        self._handles: list[IO[str]] = []
        self._kernel_fds: list[int] = []

    @staticmethod
    def _acquire_kernel_lease(identity_digest: str) -> int:
        """Reserve one unlink-proof Linux OFD byte-range identity lock."""

        fcntl = _locking_api()
        lock_fd = os.open("/dev/null", os.O_RDWR | os.O_CLOEXEC)
        offset = int(identity_digest, 16) % ((1 << 63) - 1)
        flock = struct.pack("hhqqi4x", fcntl.F_WRLCK, os.SEEK_SET, offset, 1, 0)
        try:
            fcntl.fcntl(lock_fd, fcntl.F_OFD_SETLK, flock)
        except OSError as exc:
            os.close(lock_fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeLeaseError(
                    "account-exclusive runtime is already owned by another process"
                ) from exc
            raise RuntimeLeaseError("cannot acquire account-exclusive kernel lease") from exc
        return lock_fd

    def acquire(self) -> None:
        fcntl = _locking_api()
        if self._handles or self._kernel_fds:
            raise RuntimeLeaseError("account-exclusive runtime lease is already held")
        account_kernel_digest = sha256(
            f"account:{self.account_identity_digest}".encode("ascii")
        ).hexdigest()
        kernel_digests = tuple(sorted({account_kernel_digest, self.runtime_identity_digest}))
        kernel_fds: list[int] = []
        try:
            for digest in kernel_digests:
                kernel_fds.append(self._acquire_kernel_lease(digest))
        except Exception:
            for kernel_fd in reversed(kernel_fds):
                os.close(kernel_fd)
            raise
        acquired: list[IO[str]] = []
        try:
            for path in self.paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("a+", encoding="utf-8")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    handle.close()
                    raise RuntimeLeaseError(
                        "account-exclusive runtime is already owned by another process"
                    ) from exc
                handle.seek(0)
                handle.truncate()
                json.dump(
                    {
                        "pid": os.getpid(),
                        "role": self.role,
                        "account_identity_digest": self.account_identity_digest,
                    },
                    handle,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
                acquired.append(handle)
        except Exception:
            for cleanup_handle in reversed(acquired):
                fcntl.flock(cleanup_handle.fileno(), fcntl.LOCK_UN)
                cleanup_handle.close()
            for kernel_fd in reversed(kernel_fds):
                os.close(kernel_fd)
            raise
        self._handles = acquired
        self._kernel_fds = kernel_fds

    def release(self) -> None:
        if not self._handles and not self._kernel_fds:
            return
        fcntl = _locking_api()
        for handle in reversed(self._handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self._handles = []
        for kernel_fd in reversed(self._kernel_fds):
            os.close(kernel_fd)
        self._kernel_fds = []

    def authorizes_technical_activation(
        self,
        account_identity_digest: str,
        runtime_dir: str | Path,
    ) -> bool:
        """Prove this live object still holds the exact account/runtime lease."""

        runtime = Path(os.path.realpath(runtime_dir))
        runtime_digest = sha256(f"runtime:{runtime}".encode()).hexdigest()
        return bool(
            self._handles
            and self._kernel_fds
            and account_identity_digest == self.account_identity_digest
            and runtime_digest == self.runtime_identity_digest
        )

    def __enter__(self) -> AccountExclusiveRuntimeLease:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.release()
