"""Small hardened JSON-file primitives for machine-local operational artifacts."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .durable_file_creation import canonical_file_path


class DurableJsonError(OSError):
    """A local JSON artifact path or durable operation is unsafe/untrusted."""


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DurableJsonError("operational artifact is not canonical JSON") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DurableJsonError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def read_regular_json(path: str | Path, *, label: str) -> dict[str, Any]:
    canonical = canonical_file_path(path)
    descriptor: int | None = None
    try:
        metadata = os.lstat(canonical)
        if stat.S_ISLNK(metadata.st_mode):
            raise DurableJsonError(f"{label} path symlink is forbidden")
        if not stat.S_ISREG(metadata.st_mode):
            raise DurableJsonError(f"{label} path must be a regular file")
        descriptor = os.open(
            canonical,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise DurableJsonError(f"{label} path identity changed during read")
        raw_bytes = _read_all(descriptor)
    except FileNotFoundError:
        raise
    except DurableJsonError:
        raise
    except OSError as exc:
        raise DurableJsonError(f"{label} cannot be read safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        decoded = raw_bytes.decode("utf-8")
        raw = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except DurableJsonError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DurableJsonError(f"{label} is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise DurableJsonError(f"{label} root must be a JSON object")
    return raw


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_replace_regular(path: str | Path, payload: bytes, *, label: str) -> Path:
    """Durably replace one regular file without following a target symlink."""

    canonical = canonical_file_path(path)
    try:
        canonical.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DurableJsonError(f"{label} parent directory is unavailable") from exc
    try:
        existing = os.lstat(canonical)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise DurableJsonError(f"{label} target cannot be inspected") from exc
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise DurableJsonError(f"{label} target symlink is forbidden")
        if not stat.S_ISREG(existing.st_mode):
            raise DurableJsonError(f"{label} target must be a regular file")

    temporary: Path | None = None
    descriptor: int | None = None
    try:
        with NamedTemporaryFile("wb", dir=canonical.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, canonical)
        temporary = None
        descriptor = os.open(
            canonical,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise DurableJsonError(f"{label} replacement is not a regular file")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        _fsync_directory(canonical.parent)
        return canonical
    except DurableJsonError:
        raise
    except OSError as exc:
        raise DurableJsonError(f"{label} durable replace failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
