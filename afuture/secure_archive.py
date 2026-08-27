"""Deterministic, fail-closed archive primitives for local production evidence.

Only regular files are supported.  Archives are canonical uncompressed USTAR streams:
member order, metadata and end padding are deterministic, and verification reserializes
accepted members byte-for-byte so truncation, appended garbage and metadata drift fail.
"""

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


@dataclass(frozen=True)
class VerifiedArchive:
    members: Mapping[str, bytes]
    archive_sha256: str
    total_member_bytes: int


def _canonical_member_name(name: object) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise SecureArchiveError("archive member name is invalid")
    if name.startswith("/"):
        raise SecureArchiveError("archive member path must be relative")
    pure = PurePosixPath(name)
    parts = pure.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise SecureArchiveError("archive member path traversal is forbidden")
    canonical = pure.as_posix()
    if canonical != name:
        raise SecureArchiveError("archive member path is not canonical")
    return canonical


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


def _archive_bytes(
    members: Mapping[str, bytes],
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    max_members: int,
) -> bytes:
    if not members or len(members) > max_members:
        raise SecureArchiveError("archive member count is invalid")
    normalized: dict[str, bytes] = {}
    total = 0
    for raw_name, raw_payload in members.items():
        name = _canonical_member_name(raw_name)
        if name in normalized:
            raise SecureArchiveError("archive contains duplicate member names")
        if not isinstance(raw_payload, bytes):
            raise SecureArchiveError("archive member payload must be bytes")
        if len(raw_payload) > max_member_bytes:
            raise SecureArchiveError(f"archive member exceeds size limit: {name}")
        total += len(raw_payload)
        if total > max_total_bytes:
            raise SecureArchiveError("archive payload exceeds total size limit")
        normalized[name] = raw_payload

    buffer = io.BytesIO()
    try:
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as handle:
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
                handle.addfile(info, io.BytesIO(payload))
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise SecureArchiveError("cannot build deterministic archive") from exc
    return buffer.getvalue()


def build_deterministic_archive(
    path: str | Path,
    members: Mapping[str, bytes],
    *,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> str:
    """Publish one canonical archive without replacing an existing output path."""

    target = Path(path)
    if target == Path(target.anchor) or not target.name:
        raise SecureArchiveError("archive output path is unsafe")
    if any(value <= 0 for value in (max_member_bytes, max_total_bytes, max_members)):
        raise SecureArchiveError("archive size limits must be positive")
    encoded = _archive_bytes(
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
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
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
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        temporary.unlink()
        temporary = None
        return sha256(encoded).hexdigest()
    except SecureArchiveError:
        raise
    except OSError as exc:
        if linked:
            # Publication durability is ambiguous.  Preserve the visible file as incident evidence.
            raise SecureArchiveError("archive publication durability is ambiguous") from exc
        raise SecureArchiveError("archive publication failed") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read_archive_bytes(path: Path, *, maximum: int) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
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
            raise SecureArchiveError("archive exceeds total size limit")
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
    """Validate a canonical deterministic archive without modifying any files."""

    if any(value <= 0 for value in (max_member_bytes, max_total_bytes, max_members)):
        raise SecureArchiveError("archive size limits must be positive")
    allowed = None
    if allowed_members is not None:
        allowed = {_canonical_member_name(item) for item in allowed_members}
    required = (
        set() if required_members is None else {_canonical_member_name(item) for item in required_members}
    )
    if allowed is not None and not required.issubset(allowed):
        raise SecureArchiveError("required archive members are not allowlisted")

    # USTAR payload may include padding, so permit a bounded metadata multiple above member bytes.
    maximum_archive_bytes = max_total_bytes + max_members * 2048 + 20 * _ARCHIVE_BLOCK
    encoded = _read_archive_bytes(Path(path), maximum=maximum_archive_bytes)
    members: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(encoded), mode="r:") as handle:
            infos = handle.getmembers()
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
                    raise SecureArchiveError(f"archive member exceeds size limit: {name}")
                extracted = handle.extractfile(info)
                if extracted is None:
                    raise SecureArchiveError("archive member cannot be read")
                payload = extracted.read(max_member_bytes + 1)
                if len(payload) != info.size or len(payload) > max_member_bytes:
                    raise SecureArchiveError("archive member is truncated or oversized")
                total += len(payload)
                if total > max_total_bytes:
                    raise SecureArchiveError("archive payload exceeds total size limit")
                members[name] = payload
    except SecureArchiveError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        raise SecureArchiveError("archive format is invalid or truncated") from exc

    if not required.issubset(members):
        missing = sorted(required - set(members))
        raise SecureArchiveError("archive required members are missing: " + ", ".join(missing))

    canonical = _archive_bytes(
        members,
        max_member_bytes=max_member_bytes,
        max_total_bytes=max_total_bytes,
        max_members=max_members,
    )
    if canonical != encoded:
        raise SecureArchiveError(
            "archive bytes are non-canonical, truncated, tampered, or contain trailing data"
        )
    return VerifiedArchive(
        members=dict(members),
        archive_sha256=sha256(encoded).hexdigest(),
        total_member_bytes=total,
    )
