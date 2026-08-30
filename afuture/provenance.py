"""Local Git, filesystem and interpreter provenance helpers."""

from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from .secure_archive import canonical_json_bytes

_SHA40 = re.compile(r"[0-9a-f]{40}")


class ProvenanceError(RuntimeError):
    """Local deployment provenance cannot be established safely."""


def canonical_safe_path(path: str | Path, *, label: str) -> Path:
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw:
        raise ProvenanceError(f"{label} path is invalid")
    canonical = Path(os.path.realpath(os.path.abspath(raw)))
    if canonical == Path(canonical.anchor):
        raise ProvenanceError(f"{label} path must not be a filesystem root")
    return canonical


def read_regular_file(path: str | Path, *, maximum: int = 512 * 1024 * 1024) -> bytes:
    target = Path(path)
    if maximum <= 0:
        raise ProvenanceError("file size limit must be positive")
    try:
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise ProvenanceError(f"cannot open regular file safely: {target}") from exc
    try:
        opened = os.fstat(descriptor)
        visible = os.lstat(target)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != visible.st_dev
            or opened.st_ino != visible.st_ino
            or opened.st_size < 0
            or opened.st_size > maximum
        ):
            raise ProvenanceError(f"file identity or size is invalid: {target}")
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
            raise ProvenanceError(f"file exceeds size limit: {target}")
        return payload
    finally:
        os.close(descriptor)


def file_sha256(path: str | Path, *, maximum: int = 512 * 1024 * 1024) -> str:
    return sha256(read_regular_file(path, maximum=maximum)).hexdigest()


def verify_frozen_input_files(
    directory: str | Path,
    expected_sha256: Mapping[str, str],
    *,
    maximum: int = 512 * 1024 * 1024,
) -> tuple[dict[str, bytes], list[dict[str, str | int]]]:
    """Read exact frozen basenames safely and return their verified bytes and manifest."""

    if not isinstance(expected_sha256, Mapping) or not expected_sha256:
        raise ProvenanceError("frozen input digest manifest is invalid")
    root = Path(directory)
    verified: dict[str, bytes] = {}
    manifest: list[dict[str, str | int]] = []
    for basename in sorted(expected_sha256):
        expected = expected_sha256[basename]
        if (
            not isinstance(basename, str)
            or Path(basename).name != basename
            or not basename
            or not isinstance(expected, str)
            or len(expected) != 64
            or any(char not in "0123456789abcdef" for char in expected)
        ):
            raise ProvenanceError("frozen input digest manifest is invalid")
        payload = read_regular_file(root / basename, maximum=maximum)
        actual = sha256(payload).hexdigest()
        if actual != expected:
            raise ProvenanceError(
                f"frozen input SHA-256 mismatch: {basename}; expected={expected}, actual={actual}"
            )
        verified[basename] = payload
        manifest.append(
            {
                "basename": basename,
                "sha256": actual,
                "size_bytes": len(payload),
            }
        )
    return verified, manifest


def _git_binary() -> str:
    binary = shutil.which("git")
    if binary is None:
        raise ProvenanceError("git executable is unavailable")
    return binary


def _git(repo: Path, *args: str, text: bool = True) -> str | bytes:
    try:
        result = subprocess.run(
            [_git_binary(), "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=text,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvenanceError("git provenance command failed") from exc
    return result.stdout


def repository_root(start: str | Path | None = None) -> Path:
    seed = Path.cwd() if start is None else Path(start)
    raw = str(_git(seed, "rev-parse", "--show-toplevel")).strip()
    root = canonical_safe_path(raw, label="repository")
    if not (root / ".git").exists() and not (root / ".git").is_file():
        raise ProvenanceError("source is not a Git checkout")
    return root


def git_head(repo: str | Path) -> str:
    root = canonical_safe_path(repo, label="repository")
    head = str(_git(root, "rev-parse", "HEAD")).strip().lower()
    if _SHA40.fullmatch(head) is None:
        raise ProvenanceError("Git HEAD is not a full 40-character commit")
    return head


def require_clean_tracked_worktree(repo: str | Path) -> None:
    root = canonical_safe_path(repo, label="repository")
    status = str(_git(root, "status", "--porcelain", "--untracked-files=no"))
    if status.strip():
        raise ProvenanceError("tracked Git worktree is dirty")


def production_source_tree_digest(repo: str | Path) -> tuple[str, tuple[tuple[str, str], ...]]:
    root = canonical_safe_path(repo, label="repository")
    raw = _git(
        root,
        "ls-files",
        "-z",
        "--",
        "afuture",
        "config",
        "constraints",
        "pyproject.toml",
        text=False,
    )
    assert isinstance(raw, bytes)
    names = [item.decode("utf-8") for item in raw.split(b"\x00") if item]
    if not names or "pyproject.toml" not in names:
        raise ProvenanceError("production tracked source manifest is incomplete")
    entries: list[tuple[str, str]] = []
    for name in sorted(names):
        path = root / name
        payload = read_regular_file(path)
        entries.append((name, sha256(payload).hexdigest()))
    digest = sha256(canonical_json_bytes(entries)).hexdigest()
    return digest, tuple(entries)


def git_commit_is_ancestor(repo: str | Path, ancestor: str, descendant: str) -> bool:
    root = canonical_safe_path(repo, label="repository")
    if _SHA40.fullmatch(ancestor) is None or _SHA40.fullmatch(descendant) is None:
        raise ProvenanceError("Git compatibility commits must be full SHA-1 values")
    try:
        result = subprocess.run(
            [_git_binary(), "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvenanceError("Git ancestry check failed") from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise ProvenanceError("Git ancestry check failed")


def interpreter_identity() -> dict[str, str]:
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "os": platform.system(),
        "architecture": platform.machine(),
        "executable": str(Path(sys.executable).resolve(strict=False)),
    }


def native_module_identity(package: str = "vnpy_ctp") -> dict[str, object] | None:
    """Bind every native extension shipped by a package into one stable identity."""

    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None:
        return None
    candidates: list[Path] = []
    suffixes = {".so", ".pyd", ".dylib"}
    locations = list(spec.submodule_search_locations or ())
    for raw_location in locations:
        location = Path(raw_location)
        try:
            candidates.extend(
                path
                for path in location.rglob("*")
                if path.is_file() and any(str(path).endswith(suffix) for suffix in suffixes)
            )
        except OSError:
            continue
    if not candidates and spec.origin and spec.origin not in {"built-in", "frozen"}:
        origin = Path(spec.origin)
        if any(str(origin).endswith(suffix) for suffix in suffixes):
            candidates = [origin]
    if not candidates:
        return None

    files: list[dict[str, str]] = []
    seen: set[Path] = set()
    for candidate in sorted(candidates, key=os.fspath):
        target = canonical_safe_path(candidate, label="native module")
        if target in seen:
            continue
        seen.add(target)
        files.append({"path": str(target), "sha256": file_sha256(target)})
    if not files:
        return None
    return {
        "files": files,
        "sha256": sha256(canonical_json_bytes(files)).hexdigest(),
    }
