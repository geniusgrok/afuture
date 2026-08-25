"""结构化交易审计日志。"""

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, cast

from .jsonl import DEFAULT_JSONL_BACKUP_COUNT, DEFAULT_JSONL_MAX_BYTES, RotatingJsonlWriter


class AuditJournal:
    """逐行记录信号、订单、成交和风险事件。"""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_JSONL_MAX_BYTES,
        backup_count: int = DEFAULT_JSONL_BACKUP_COUNT,
    ) -> None:
        self.path = Path(path)
        self._writer = RotatingJsonlWriter(
            self.path,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )

    def record(
        self,
        event_type: str,
        payload: object,
        *,
        timestamp: datetime | None = None,
    ) -> None:
        row = {
            "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
            "event_type": event_type,
            "payload": _to_jsonable(payload),
        }
        self._writer.write_line(
            json.dumps(
                row,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )


def _to_jsonable(value: object) -> object:
    # ``is_dataclass`` also returns true for dataclass *types*; audit payloads
    # contain instances only, and passing a type to asdict is an error.
    if not isinstance(value, type) and is_dataclass(value):
        return _to_jsonable(asdict(cast(Any, value)))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value
