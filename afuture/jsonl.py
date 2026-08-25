"""Bounded append-only JSONL storage for local operational evidence."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

DEFAULT_JSONL_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_JSONL_BACKUP_COUNT = 14


class RotatingJsonlWriter:
    """Append complete UTF-8 lines and retain a bounded set of numbered backups.

    Rotation is process-local and occurs before an append would exceed ``max_bytes``.
    A single event larger than the limit is retained intact in the current file.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_JSONL_MAX_BYTES,
        backup_count: int = DEFAULT_JSONL_BACKUP_COUNT,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backup_count <= 0:
            raise ValueError("backup_count must be positive")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = Lock()

    def write_line(self, line: str) -> None:
        """Append one serialized JSON value without permitting embedded line breaks."""
        if not line or "\n" in line or "\r" in line:
            raise ValueError("JSONL record must be one non-empty line")
        encoded = (line + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            current_size = self.path.stat().st_size if self.path.exists() else 0
            if current_size and current_size + len(encoded) > self.max_bytes:
                self._rotate()
            with self.path.open("ab") as handle:
                handle.write(encoded)

    def _rotate(self) -> None:
        oldest = self._backup_path(self.backup_count)
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                source.replace(self._backup_path(index + 1))
        self.path.replace(self._backup_path(1))

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")
