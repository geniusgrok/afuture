"""Create-only durable files with verifiable invocation ownership."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import struct
from collections.abc import Iterator
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
def durable_file_lock(path: str | Path) -> Iterator[Path]:
    """Serialize all owner operations for one canonical durable file path."""

    canonical = canonical_file_path(path)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    descriptor = _kernel_lock_descriptor(canonical, namespace="afuture-durable-file")
    body_failed = False
    try:
        yield canonical
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if not body_failed:
                raise DurableFileError("durable file lock cleanup failed") from exc


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


def create_durable_file_exclusive(path: str | Path, payload: bytes) -> DurableFileCreationToken:
    """Create ``path`` with O_EXCL and return exact inode/content ownership proof."""

    with durable_file_lock(path) as canonical:
        descriptor: int | None = None
        body_failed = False
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
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            visible = os.stat(canonical, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(visible.st_mode)
                or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
            ):
                raise DurableFileError("durable file path was replaced during creation")
            _fsync_parent(canonical)
            return DurableFileCreationToken(
                path=canonical,
                device=opened.st_dev,
                inode=opened.st_ino,
                size=len(payload),
                payload_sha256=sha256(payload).hexdigest(),
            )
        except BaseException:
            body_failed = True
            raise
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    if not body_failed:
                        raise DurableFileError("durable file creation cleanup failed") from exc


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


def creation_token_matches(token: DurableFileCreationToken) -> bool:
    """Validate exact current path identity and bytes under the owner path lock."""

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
            return (visible.st_dev, visible.st_ino) == (token.device, token.inode)
        except (FileNotFoundError, OSError):
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)


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
