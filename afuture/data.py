"""CSV Tick 行情读取与回放数据校验。"""

import csv
from datetime import datetime
from pathlib import Path

from .models import Tick

_REQUIRED_COLUMNS = {
    "timestamp",
    "symbol",
    "exchange",
    "bid_price",
    "ask_price",
    "last_price",
    "bid_volume",
    "ask_volume",
    "trading_day",
}


def read_ticks(
    path: str | Path,
    *,
    sort_rows: bool = True,
) -> list[Tick]:
    """读取标准化 Tick CSV。

    回放默认按时间排序；``data-check`` 可关闭排序以检查源文件本身是否乱序。
    """
    ticks: list[Tick] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = _REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"missing columns: {sorted(missing)}")

        for row in reader:
            tick = Tick(
                symbol=row["symbol"],
                exchange=row["exchange"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                bid_price=float(row["bid_price"]),
                ask_price=float(row["ask_price"]),
                last_price=float(row["last_price"]),
                bid_volume=float(row["bid_volume"]),
                ask_volume=float(row["ask_volume"]),
                trading_day=row["trading_day"],
                limit_up=float(row.get("limit_up") or 0.0),
                limit_down=float(row.get("limit_down") or 0.0),
                volume=float(row.get("volume") or 0.0),
                open_interest=float(row.get("open_interest") or 0.0),
            )
            tick.validate()
            ticks.append(tick)
    return sorted(ticks, key=lambda item: item.timestamp) if sort_rows else ticks
