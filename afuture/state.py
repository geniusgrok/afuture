"""带版本、序列和校验和的运行状态持久化。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from math import isfinite
import os
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


class StateIntegrityError(ValueError):
    """Persisted runtime state cannot be trusted or safely advanced."""


@dataclass(frozen=True)
class _DecodedState:
    state: RuntimeState
    sequence: int
    legacy: bool


class StateStore:
    """使用原子替换；任何校验失败都拒绝加载而不是猜测恢复。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> RuntimeState:
        if not self.path.exists():
            return RuntimeState()
        return self._read_verified().state

    def _read_verified(self) -> _DecodedState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateIntegrityError("invalid state JSON") from exc
        if not isinstance(raw, dict):
            raise StateIntegrityError("state root must be a JSON object")
        if "schema_version" not in raw:
            # 兼容旧版裸 RuntimeState JSON。
            state = self._state_from_payload(raw)
            return _DecodedState(state, 0, True)

        required = {"schema_version", "sequence", "state", "checksum"}
        missing = sorted(required.difference(raw))
        if missing:
            raise StateIntegrityError(
                "state envelope missing fields: " + ", ".join(missing)
            )
        schema_version = raw["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version <= 0
        ):
            raise StateIntegrityError(
                "state schema version must be a positive integer"
            )
        if schema_version > SCHEMA_VERSION:
            raise StateIntegrityError(
                "state schema version is newer than this program"
            )
        sequence = raw["sequence"]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
        ):
            raise StateIntegrityError("state sequence must be a positive integer")
        state_payload = raw["state"]
        if not isinstance(state_payload, dict):
            raise StateIntegrityError(
                "state payload must be a JSON object"
            )
        expected = self._checksum(schema_version, sequence, state_payload)
        if expected != raw.get("checksum"):
            raise StateIntegrityError("state checksum mismatch")
        state = self._state_from_payload(state_payload)
        return _DecodedState(state, sequence, False)

    @staticmethod
    def _state_from_payload(payload: dict) -> RuntimeState:
        bool_fields = {"kill_switch", "reconciled", "metadata_verified"}
        string_fields = {
            "kill_reason",
            "trading_day",
            "runtime_mode",
            "reduce_reason",
            "last_order_id",
            "last_trade_id",
            "directional_daily_circuit_day",
            "last_account_trading_day",
        }
        number_fields = {
            "day_start_equity",
            "equity_high_watermark",
            "last_account_equity",
        }
        list_fields = {"positions", "recent_daily_returns"}
        object_fields = {"strategy_states", "auto_pairs"}

        for name in bool_fields.intersection(payload):
            if not isinstance(payload[name], bool):
                raise StateIntegrityError(f"state field {name} must be bool")
        for name in string_fields.intersection(payload):
            if not isinstance(payload[name], str):
                raise StateIntegrityError(f"state field {name} must be string")
        for name in number_fields.intersection(payload):
            value = payload[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
            ):
                raise StateIntegrityError(
                    f"state field {name} must be finite number"
                )
        for name in list_fields.intersection(payload):
            if not isinstance(payload[name], list):
                raise StateIntegrityError(f"state field {name} must be a list")
        for name in object_fields.intersection(payload):
            if not isinstance(payload[name], dict):
                raise StateIntegrityError(f"state field {name} must be an object")

        positions = payload.get("positions", [])
        if any(not isinstance(item, dict) for item in positions):
            raise StateIntegrityError(
                "state field positions must contain only objects"
            )
        for item in positions:
            try:
                position = ContractPosition(**item)
            except (TypeError, ValueError) as exc:
                raise StateIntegrityError(
                    "invalid persisted position"
                ) from exc
            if not isinstance(position.symbol, str) or not isinstance(
                position.exchange,
                str,
            ):
                raise StateIntegrityError("invalid persisted position")
            buckets = (
                position.long_today,
                position.long_yesterday,
                position.short_today,
                position.short_yesterday,
            )
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in buckets
            ):
                raise StateIntegrityError("invalid persisted position")
        for name in object_fields:
            values = payload.get(name, {})
            if any(not isinstance(value, dict) for value in values.values()):
                raise StateIntegrityError(
                    f"state field {name} values must be objects"
                )
        recent_returns = payload.get("recent_daily_returns", [])
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            for value in recent_returns
        ):
            raise StateIntegrityError(
                "state field recent_daily_returns must contain finite numbers"
            )
        runtime_mode = payload.get("runtime_mode", RuntimeMode.RUNNING.value)
        if runtime_mode not in {item.value for item in RuntimeMode}:
            raise StateIntegrityError("state field runtime_mode is unsupported")

        allowed = RuntimeState.__dataclass_fields__
        return RuntimeState(
            **{key: value for key, value in payload.items() if key in allowed}
        )

    def save(self, state: RuntimeState) -> None:
        sequence = 1
        if self.path.exists():
            sequence = self._read_verified().sequence + 1
        state_payload = asdict(state)
        self._state_from_payload(state_payload)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "state": state_payload,
        }
        envelope["checksum"] = self._checksum(SCHEMA_VERSION, sequence, state_payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

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
