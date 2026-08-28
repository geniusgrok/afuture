"""Deterministic, fail-closed archive primitives for local production evidence."""

from __future__ import annotations

import errno
import io
import os
import stat
import tarfile
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

DEFAULT_MAX_MEMBER_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_MEMBERS = 4096
_ARCHIVE_BLOCK = 512


class SecureArchiveError(RuntimeError):
    """Archive bytes or filesystem publication cannot be trusted."""


ArchiveSecurityError = SecureArchiveError


@dataclass(frozen=True)
class VerifiedArchive:
    members: Mapping[str, bytes]
    archive_sha256: str
    total_member_bytes: int


def canonical_json_bytes(value: object) -> bytes:
    import json

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SecureArchiveError("value is not canonical JSON") from exc


def _canonical_member_name(name: object) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise SecureArchiveError("archive member name is invalid")
    if name.startswith("/"):
        raise SecureArchiveError("archive member path must be relative")
    pure = PurePosixPath(name)
    if not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise SecureArchiveError("archive member path traversal is forbidden")
    canonical = pure.as_posix()
    if canonical != name:
        raise SecureArchiveError("archive member path is not canonical")
    return canonical


def _validate_limits(max_member_bytes: int, max_total_bytes: int, max_members: int) -> None:
    if any(value <= 0 for value in (max_member_bytes, max_total_bytes, max_members)):
        raise SecureArchiveError("archive size limits must be positive")


def create_deterministic_archive(
    members: Mapping[str, bytes],
    *,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> bytes:
    """Return canonical uncompressed USTAR bytes for regular-file members only."""

    _validate_limits(max_member_bytes, max_total_bytes, max_members)
    if not members or len(members) > max_members:
        raise SecureArchiveError("archive member count is invalid")
    normalized: dict[str, bytes] = {}
    total = 0
    for raw_name, payload in members.items():
        name = _canonical_member_name(raw_name)
        if name in normalized:
            raise SecureArchiveError("archive contains duplicate member names")
        if not isinstance(payload, bytes):
            raise SecureArchiveError("archive member payload must be bytes")
        if len(payload) > max_member_bytes:
            raise SecureArchiveError(f"archive member size limit exceeded: {name}")
        total += len(payload)
        if total > max_total_bytes:
            raise SecureArchiveError("archive total size limit exceeded")
        normalized[name] = payload

    stream = io.BytesIO()
    try:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name in sorted(normalized):
                payload = normalized[name]
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = 0o600
                info.type = tarfile.REGTYPE
                info.linkname = ""
                archive.addfile(info, io.BytesIO(payload))
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise SecureArchiveError("cannot build deterministic archive") from exc
    return stream.getvalue()


def read_deterministic_archive(
    payload: bytes,
    *,
    allowed_members: Collection[str] | None = None,
    required_members: Collection[str] | None = None,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> dict[str, bytes]:
    """Validate canonical archive bytes and return exact member payloads."""

    _validate_limits(max_member_bytes, max_total_bytes, max_members)
    if not isinstance(payload, bytes) or not payload:
        raise SecureArchiveError("archive payload is empty or invalid")
    allowed = (
        None
        if allowed_members is None
        else {_canonical_member_name(name) for name in allowed_members}
    )
    required = (
        set()
        if required_members is None
        else {_canonical_member_name(name) for name in required_members}
    )
    if allowed is not None and not required.issubset(allowed):
        raise SecureArchiveError("required archive members are not allowlisted")

    members: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            infos = archive.getmembers()
            if not infos or len(infos) > max_members:
                raise SecureArchiveError("archive member count is invalid")
            for info in infos:
                name = _canonical_member_name(info.name)
                if name in members:
                    raise SecureArchiveError("archive contains duplicate member names")
                if allowed is not None and name not in allowed:
                    raise SecureArchiveError(f"archive contains unknown member: {name}")
                if info.issym():
                    raise SecureArchiveError("archive symlink members are forbidden")
                if info.islnk():
                    raise SecureArchiveError("archive hardlink members are forbidden")
                if not info.isfile() or info.type != tarfile.REGTYPE:
                    raise SecureArchiveError("archive contains non-regular member")
                if info.size < 0 or info.size > max_member_bytes:
                    raise SecureArchiveError(f"archive member size limit exceeded: {name}")
                extracted = archive.extractfile(info)
                if extracted is None:
                    raise SecureArchiveError("archive member cannot be read")
                data = extracted.read(max_member_bytes + 1)
                if len(data) != info.size or len(data) > max_member_bytes:
                    raise SecureArchiveError("archive member is truncated or oversized")
                total += len(data)
                if total > max_total_bytes:
                    raise SecureArchiveError("archive total size limit exceeded")
                members[name] = data
    except SecureArchiveError:
        raise
    except (EOFError, OSError, tarfile.TarError, ValueError) as exc:
        raise SecureArchiveError("archive format is invalid or truncated") from exc

    if not required.issubset(members):
        missing = sorted(required - set(members))
        raise SecureArchiveError("archive required members are missing: " + ", ".join(missing))
    canonical = create_deterministic_archive(
        members,
        max_member_bytes=max_member_bytes,
        max_total_bytes=max_total_bytes,
        max_members=max_members,
    )
    if canonical != payload:
        raise SecureArchiveError(
            "archive bytes are non-canonical, truncated, tampered, or contain trailing data"
        )
    return members


def _read_archive_file(path: Path, maximum: int) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise SecureArchiveError("archive cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        visible = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != visible.st_dev
            or opened.st_ino != visible.st_ino
            or opened.st_size <= 0
            or opened.st_size > maximum
        ):
            raise SecureArchiveError("archive file identity or size is invalid")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise SecureArchiveError("archive total size limit exceeded")
        return payload
    finally:
        os.close(descriptor)


def verify_archive(
    path: str | Path,
    *,
    allowed_members: Collection[str] | None = None,
    required_members: Collection[str] | None = None,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> VerifiedArchive:
    """Verify a canonical deterministic archive file without modifying it."""

    _validate_limits(max_member_bytes, max_total_bytes, max_members)
    maximum = max_total_bytes + max_members * 2048 + 20 * _ARCHIVE_BLOCK
    payload = _read_archive_file(Path(path), maximum)
    members = read_deterministic_archive(
        payload,
        allowed_members=allowed_members,
        required_members=required_members,
        max_member_bytes=max_member_bytes,
        max_total_bytes=max_total_bytes,
        max_members=max_members,
    )
    return VerifiedArchive(
        members=members,
        archive_sha256=sha256(payload).hexdigest(),
        total_member_bytes=sum(len(data) for data in members.values()),
    )


def build_deterministic_archive(
    path: str | Path,
    members: Mapping[str, bytes],
    *,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> str:
    """Durably publish a new deterministic archive without overwriting an existing path."""

    target = Path(path)
    if target == Path(target.anchor) or not target.name:
        raise SecureArchiveError("archive output path is unsafe")
    payload = create_deterministic_archive(
        members,
        max_member_bytes=max_member_bytes,
        max_total_bytes=max_total_bytes,
        max_members=max_members,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    linked = False
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temp_name)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "archive write made no progress")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise SecureArchiveError("archive output already exists") from exc
        linked = True
        directory = os.open(
            target.parent,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        temporary.unlink()
        temporary = None
        return sha256(payload).hexdigest()
    except SecureArchiveError:
        raise
    except OSError as exc:
        if linked:
            raise SecureArchiveError("archive publication durability is ambiguous") from exc
        raise SecureArchiveError("archive publication failed") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
