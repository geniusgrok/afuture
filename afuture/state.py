"""带版本、序列和校验和的运行状态持久化。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import ContractPosition, RuntimeMode

SCHEMA_VERSION = 2


@dataclass
class RuntimeState:
    kill_switch: bool = False
    kill_reason: str = ""
    reconciled: bool = False
    trading_day: str = ""
    day_start_equity: float = 0.0
    equity_high_watermark: float = 0.0
    positions: list[dict] = field(default_factory=list)
    strategy_states: dict[str, dict] = field(default_factory=dict)
    auto_pairs: dict[str, dict] = field(default_factory=dict)
    runtime_mode: str = RuntimeMode.RUNNING.value
    reduce_reason: str = ""
    metadata_verified: bool = False
    last_order_id: str = ""
    last_trade_id: str = ""
    recent_daily_returns: list[float] = field(default_factory=list)
    directional_daily_circuit_day: str = ""
    last_account_equity: float = 0.0
    last_account_trading_day: str = ""


class StateStore:
    """使用原子替换；任何校验失败都拒绝加载而不是猜测恢复。"""
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> RuntimeState:
        if not self.path.exists():
            return RuntimeState()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if "schema_version" not in raw:
            # 兼容旧版裸 RuntimeState JSON。
            allowed = RuntimeState.__dataclass_fields__
            return RuntimeState(**{k: v for k, v in raw.items() if k in allowed})
        if int(raw.get("schema_version", 0)) > SCHEMA_VERSION:
            raise ValueError("state schema version is newer than this program")
        expected = self._checksum(raw["schema_version"], raw["sequence"], raw["state"])
        if expected != raw.get("checksum"):
            raise ValueError("state checksum mismatch")
        allowed = RuntimeState.__dataclass_fields__
        return RuntimeState(**{k: v for k, v in raw["state"].items() if k in allowed})

    def save(self, state: RuntimeState) -> None:
        sequence = 1
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                sequence = int(raw.get("sequence", 0)) + 1 if "schema_version" in raw else 1
            except Exception:
                sequence = 1
        state_payload = asdict(state)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "state": state_payload,
        }
        envelope["checksum"] = self._checksum(SCHEMA_VERSION, sequence, state_payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True))
            temp_path = Path(handle.name)
        temp_path.replace(self.path)

    def save_positions(self, state: RuntimeState, positions: list[ContractPosition]) -> None:
        state.positions = [asdict(position) for position in positions]
        self.save(state)

    def positions_from_state(self, state: RuntimeState) -> list[ContractPosition]:
        return [ContractPosition(**item) for item in state.positions]

    @staticmethod
    def can_clear_kill_switch(state: RuntimeState) -> bool:
        return state.kill_switch and state.reconciled and state.metadata_verified

    @staticmethod
    def _checksum(schema_version: int, sequence: int, state: dict) -> str:
        payload = json.dumps(
            {"schema_version": schema_version, "sequence": sequence, "state": state},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        return sha256(payload).hexdigest()
