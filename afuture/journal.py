"""结构化交易审计日志。"""

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, cast


class AuditJournal:
    """逐行记录信号、订单、成交和风险事件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(
        self,
        event_type: str,
        payload: object,
        *,
        timestamp: datetime | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": (
                timestamp or datetime.now(timezone.utc)
            ).isoformat(),
            "event_type": event_type,
            "payload": _to_jsonable(payload),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
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
        return {
            str(key): _to_jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value
