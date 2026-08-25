"""自动候选的有限采样历史持久化。

只保存最近 lookback+buffer 级别的采样行情，不把原始高频 Tick 或大型数据库引入个人系统。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import Tick


class MarketSampleStore:
    """按合约保存有限长度的 JSON 样本并使用原子替换。"""

    def __init__(self, root: str | Path, max_samples: int = 256) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self.root = Path(root)
        self.max_samples = max_samples

    def save(self, symbol: str, ticks: list[Tick]) -> None:
        rows = ticks[-self.max_samples :]
        payload = [self._encode(row) for row in rows]
        path = self._path(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            temp = Path(handle.name)
        temp.replace(path)

    def append(self, tick: Tick) -> None:
        rows = self.load(tick.symbol)
        if rows and tick.timestamp < rows[-1].timestamp:
            return
        if rows and tick.timestamp == rows[-1].timestamp:
            rows[-1] = tick
        else:
            rows.append(tick)
        self.save(tick.symbol, rows)

    def load(self, symbol: str) -> list[Tick]:
        path = self._path(symbol)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = [self._decode(item) for item in raw]
        except Exception:
            return []
        rows.sort(key=lambda item: item.timestamp)
        return rows[-self.max_samples :]

    def load_many(self, symbols) -> dict[str, list[Tick]]:
        return {str(symbol): self.load(str(symbol)) for symbol in symbols}

    def _path(self, symbol: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)
        return self.root / f"{safe}.json"

    @staticmethod
    def _encode(tick: Tick) -> dict:
        row = asdict(tick)
        row["timestamp"] = tick.timestamp.isoformat()
        return row

    @staticmethod
    def _decode(row: dict) -> Tick:
        payload = dict(row)
        payload["timestamp"] = datetime.fromisoformat(payload["timestamp"])
        tick = Tick(**payload)
        tick.validate()
        return tick
