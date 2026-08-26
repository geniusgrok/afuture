"""Create-only durable files with verifiable invocation ownership."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import struct
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

_F_OFD_SETLKW = getattr(fcntl, "F_OFD_SETLKW", 38)


class DurableFileError(OSError):
    """A durable create, identity check, lock, or cleanup failed."""


@dataclass(frozen=True)
class DurableFileCreationToken:
    """Proof binding one invocation to exact bytes at one inode and path."""

    path: Path
    device: int
    inode: int
    size: int
    payload_sha256: str


def canonical_file_path(path: str | Path) -> Path:
    raw_path = os.fspath(path)
    if not isinstance(raw_path, str) or not raw_path:
        raise DurableFileError("durable file path is invalid")
    absolute_path = os.path.abspath(raw_path)
    canonical_parent = Path(os.path.realpath(os.path.dirname(absolute_path)))
    return canonical_parent / os.path.basename(absolute_path)


def _kernel_lock_descriptor(path: Path, *, namespace: str) -> int:
    descriptor = os.open("/dev/null", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    identity = sha256(f"{namespace}:{canonical_file_path(path)}".encode()).digest()
    offset = int.from_bytes(identity[:8], "big") % ((1 << 63) - 1)
    lock = struct.pack("hhqqi4x", fcntl.F_WRLCK, os.SEEK_SET, offset, 1, 0)
    try:
        fcntl.fcntl(descriptor, _F_OFD_SETLKW, lock)
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise DurableFileError("durable file is locked") from exc
        raise DurableFileError("durable file kernel lock failed") from exc
    return descriptor


@contextmanager
def durable_file_locks(paths: Iterable[str | Path]) -> Iterator[tuple[Path, ...]]:
    """Acquire canonical artifact locks in the one global lexicographic order."""

    canonical_paths = tuple(sorted({canonical_file_path(path) for path in paths}, key=os.fspath))
    if not canonical_paths:
        raise DurableFileError("at least one durable file lock path is required")
    descriptors: list[int] = []
    body_failed = False
    try:
        for canonical in canonical_paths:
            canonical.parent.mkdir(parents=True, exist_ok=True)
            descriptors.append(_kernel_lock_descriptor(canonical, namespace="afuture-durable-file"))
        yield canonical_paths
    except BaseException:
        body_failed = True
        raise
    finally:
        cleanup_errors: list[OSError] = []
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_errors.append(exc)
        if cleanup_errors and not body_failed:
            raise DurableFileError("durable file lock cleanup failed") from cleanup_errors[0]


@contextmanager
def durable_file_lock(path: str | Path) -> Iterator[Path]:
    """Serialize one owner operation using the global aggregate lock order."""

    with durable_file_locks((path,)) as canonical_paths:
        yield canonical_paths[0]


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    body_failed = False
    try:
        os.fsync(descriptor)
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError:
            if not body_failed:
                raise


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise DurableFileError("durable file write made no progress")
        offset += written


def _cleanup_failed_exclusive_create(
    path: Path,
    *,
    device: int | None,
    inode: int | None,
) -> list[str]:
    if device is None or inode is None:
        return ["created file inode identity is unavailable; incident evidence preserved"]
    try:
        visible = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"created file path cannot be revalidated: {exc}"]
    if not stat.S_ISREG(visible.st_mode) or (visible.st_dev, visible.st_ino) != (device, inode):
        return ["created file path identity changed; incident evidence preserved"]
    try:
        os.unlink(path)
    except OSError as exc:
        return [f"cleanup unlink failed: {exc}"]
    try:
        _fsync_parent(path)
    except OSError as exc:
        return [f"cleanup parent fsync failed after unlink: {exc}"]
    return []


def create_durable_file_exclusive(path: str | Path, payload: bytes) -> DurableFileCreationToken:
    """Create ``path`` with O_EXCL and return exact inode/content ownership proof."""

    with durable_file_lock(path) as canonical:
        descriptor: int | None = None
        device: int | None = None
        inode: int | None = None
        token: DurableFileCreationToken | None = None
        primary_error: BaseException | None = None
        diagnostics: list[str] = []
        try:
            try:
                descriptor = os.open(
                    canonical,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError:
                raise
            opened = os.fstat(descriptor)
            device, inode = opened.st_dev, opened.st_ino
            if not stat.S_ISREG(opened.st_mode):
                raise DurableFileError("created durable file is not regular")
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            visible = os.stat(canonical, follow_symlinks=False)
            if not stat.S_ISREG(visible.st_mode) or (device, inode) != (
                visible.st_dev,
                visible.st_ino,
            ):
                raise DurableFileError("durable file path was replaced during creation")
            _fsync_parent(canonical)
            token = DurableFileCreationToken(
                path=canonical,
                device=device,
                inode=inode,
                size=len(payload),
                payload_sha256=sha256(payload).hexdigest(),
            )
        except FileExistsError:
            raise
        except BaseException as exc:
            primary_error = exc

        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                if primary_error is None:
                    primary_error = exc
                else:
                    diagnostics.append(f"descriptor close failed: {exc}")
            descriptor = None

        if primary_error is not None:
            diagnostics.extend(
                _cleanup_failed_exclusive_create(canonical, device=device, inode=inode)
            )
            message = f"durable file creation failed: {primary_error}"
            if diagnostics:
                message += "; cleanup diagnostics: " + "; ".join(diagnostics)
            raise DurableFileError(message) from primary_error
        if token is None:  # pragma: no cover - successful path always constructs a token
            raise DurableFileError("durable file creation failed without a result")
        return token


def _descriptor_matches_token(descriptor: int, token: DurableFileCreationToken) -> bool:
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (token.device, token.inode)
        or opened.st_size != token.size
    ):
        return False
    digest = sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest() == token.payload_sha256


def creation_token_matches_unlocked(token: DurableFileCreationToken) -> bool:
    """Validate a token while the caller continuously holds its artifact lock."""

    canonical = canonical_file_path(token.path)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            canonical,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        if not _descriptor_matches_token(descriptor, token):
            return False
        visible = os.stat(canonical, follow_symlinks=False)
        return (visible.st_dev, visible.st_ino) == (token.device, token.inode)
    except (FileNotFoundError, OSError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def creation_token_matches(token: DurableFileCreationToken) -> bool:
    """Validate exact current path identity and bytes under the owner path lock."""

    with durable_file_lock(token.path):
        return creation_token_matches_unlocked(token)


def unlink_created_file(token: DurableFileCreationToken) -> bool:
    """Unlink only the exact inode/content proven by ``token``, then fsync parent."""

    with durable_file_lock(token.path) as canonical:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                canonical,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            if not _descriptor_matches_token(descriptor, token):
                return False
            visible = os.stat(canonical, follow_symlinks=False)
            if (visible.st_dev, visible.st_ino) != (token.device, token.inode):
                return False
            os.unlink(canonical)
            _fsync_parent(canonical)
            return True
        except FileNotFoundError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)
