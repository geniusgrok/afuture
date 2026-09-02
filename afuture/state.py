"""带版本、序列和校验和的运行状态持久化。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import ContractPosition, RuntimeMode

SCHEMA_VERSION = 3
MAX_RECENT_TRADE_IDS = 10_000
RESERVED_STRATEGY_STATE_KEYS = frozenset(
    {
        "directional_policy_identity",
        "stress90_doctor_attestation",
        "stress90_live_permission",
        "stress90_crash_fill_recovery",
    }
)


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
    recent_trade_ids: list[str] = field(default_factory=list)
    recent_daily_returns: list[float] = field(default_factory=list)
    directional_daily_circuit_day: str = ""
    last_account_equity: float = 0.0
    last_account_trading_day: str = ""
    last_account_deposit: float = 0.0
    last_account_withdrawal: float = 0.0
    last_account_cash_flow_verified: bool = False
    last_account_settlement_id: int = -1


class StateIntegrityError(ValueError):
    """Persisted runtime state cannot be trusted or safely advanced."""


@dataclass(frozen=True)
class RuntimeStateRecord:
    state: RuntimeState
    sequence: int
    checksum: str


class StateStore:
    """使用原子替换；任何校验失败都拒绝加载而不是猜测恢复。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def previous_path(self) -> Path:
        """Return the explicit last-known-good evidence path.

        The runtime never falls back to this file automatically. Operators may inspect
        it when diagnosing a corrupt or accidentally replaced current state.
        """
        return self.path.with_name(f"{self.path.name}.prev")

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    def _require_fresh_or_current(self) -> None:
        if self.path.exists():
            return
        evidence = [path.name for path in (self.previous_path, self.lock_path) if path.exists()]
        if evidence:
            raise StateIntegrityError(
                "current runtime state is missing while incident evidence exists: "
                + ", ".join(evidence)
            )

    def load(self) -> RuntimeState:
        record = self.load_record()
        return RuntimeState() if record is None else record.state

    def load_record(self) -> RuntimeStateRecord | None:
        self._require_fresh_or_current()
        if not self.path.exists():
            return None
        return self._read_verified(self.path)

    def load_required_record(self) -> RuntimeStateRecord:
        record = self.load_record()
        if record is None:
            raise StateIntegrityError("required current runtime state is missing")
        return record

    def load_previous(self) -> RuntimeState | None:
        """Load the verified previous state without changing current-state semantics."""
        if not self.previous_path.exists():
            return None
        return self._read_verified(self.previous_path).state

    def _read_verified(self, path: Path) -> RuntimeStateRecord:
        return self._decode_verified(path.read_bytes())

    def _decode_verified(self, payload: bytes) -> RuntimeStateRecord:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StateIntegrityError("invalid state UTF-8") from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateIntegrityError("invalid state JSON") from exc
        if not isinstance(raw, dict):
            raise StateIntegrityError("state root must be a JSON object")
        required = {"schema_version", "sequence", "state", "checksum"}
        if set(raw) != required:
            raise StateIntegrityError("state envelope fields are not current")
        schema_version = raw["schema_version"]
        if schema_version != SCHEMA_VERSION:
            raise StateIntegrityError("state schema is not current")
        sequence = raw["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise StateIntegrityError("state sequence must be a positive integer")
        state_payload = raw["state"]
        if not isinstance(state_payload, dict):
            raise StateIntegrityError("state payload must be a JSON object")
        expected = self._checksum(schema_version, sequence, state_payload)
        if expected != raw.get("checksum"):
            raise StateIntegrityError("state checksum mismatch")
        state = self._state_from_payload(state_payload)
        return RuntimeStateRecord(state, sequence, raw["checksum"])

    @staticmethod
    def _state_from_payload(payload: dict) -> RuntimeState:
        current_fields = set(RuntimeState.__dataclass_fields__)
        if set(payload) != current_fields:
            raise StateIntegrityError("state payload fields are not current")
        bool_fields = {
            "kill_switch",
            "reconciled",
            "metadata_verified",
            "last_account_cash_flow_verified",
        }
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
            "last_account_deposit",
            "last_account_withdrawal",
        }
        list_fields = {"positions", "recent_daily_returns", "recent_trade_ids"}
        object_fields = {"strategy_states", "auto_pairs"}

        for name in bool_fields:
            if not isinstance(payload[name], bool):
                raise StateIntegrityError(f"state field {name} must be bool")
        for name in string_fields:
            if not isinstance(payload[name], str):
                raise StateIntegrityError(f"state field {name} must be string")
        for name in number_fields:
            value = payload[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
            ):
                raise StateIntegrityError(f"state field {name} must be finite number")
        settlement_id = payload["last_account_settlement_id"]
        if (
            isinstance(settlement_id, bool)
            or not isinstance(settlement_id, int)
            or settlement_id < -1
        ):
            raise StateIntegrityError("state field last_account_settlement_id is invalid")
        for name in list_fields:
            if not isinstance(payload[name], list):
                raise StateIntegrityError(f"state field {name} must be a list")
        for name in object_fields:
            if not isinstance(payload[name], dict):
                raise StateIntegrityError(f"state field {name} must be an object")

        positions = payload["positions"]
        if any(not isinstance(item, dict) for item in positions):
            raise StateIntegrityError("state field positions must contain only objects")
        for item in positions:
            try:
                position = ContractPosition(**item)
                position.validate()
            except (TypeError, ValueError) as exc:
                raise StateIntegrityError("invalid persisted position") from exc
        position_keys = [(str(item["symbol"]), str(item["exchange"])) for item in positions]
        if len(position_keys) != len(set(position_keys)):
            raise StateIntegrityError(
                "state field positions contains duplicate position identities"
            )
        for name in object_fields:
            values = payload[name]
            if any(not isinstance(value, dict) for value in values.values()):
                raise StateIntegrityError(f"state field {name} values must be objects")
        recent_returns = payload["recent_daily_returns"]
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
            for value in recent_returns
        ):
            raise StateIntegrityError(
                "state field recent_daily_returns must contain finite numbers"
            )
        recent_trade_ids = payload["recent_trade_ids"]
        if (
            len(recent_trade_ids) > MAX_RECENT_TRADE_IDS
            or any(not isinstance(value, str) or not value for value in recent_trade_ids)
            or len(set(recent_trade_ids)) != len(recent_trade_ids)
        ):
            raise StateIntegrityError(
                "state field recent_trade_ids must contain unique non-empty strings within limit"
            )
        runtime_mode = payload["runtime_mode"]
        if runtime_mode not in {item.value for item in RuntimeMode}:
            raise StateIntegrityError("state field runtime_mode is unsupported")

        return RuntimeState(**payload)

    def save(
        self,
        state: RuntimeState,
        *,
        expected_sequence: int | None = None,
        expected_checksum: str | None = None,
    ) -> RuntimeStateRecord:
        self._require_fresh_or_current()
        sequence = 1
        previous_bytes: bytes | None = None
        current: RuntimeStateRecord | None = None
        if self.path.exists():
            # A corrupt target is incident evidence, not an empty state.  Verify it
            # before creating a replacement so sequence history cannot silently reset.
            previous_bytes = self.path.read_bytes()
            current = self._decode_verified(previous_bytes)
            sequence = current.sequence + 1
        current_sequence = 0 if current is None else current.sequence
        current_checksum = "" if current is None else current.checksum
        if expected_sequence is not None and expected_sequence != current_sequence:
            raise StateIntegrityError("runtime state changed concurrently")
        if expected_checksum is not None and expected_checksum != current_checksum:
            raise StateIntegrityError("runtime state changed concurrently")
        state_payload = asdict(state)
        self._state_from_payload(state_payload)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "state": state_payload,
        }
        envelope["checksum"] = self._checksum(SCHEMA_VERSION, sequence, state_payload)
        encoded = json.dumps(
            envelope,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if previous_bytes is not None:
            # Preserve only state that has just passed envelope/checksum validation.
            # Backup failure aborts before replacing the authoritative current state.
            self._atomic_replace(self.previous_path, previous_bytes)
        self._atomic_replace(self.path, encoded)
        return self._decode_verified(encoded)

    @staticmethod
    def _atomic_replace(target: Path, payload: bytes) -> None:
        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "wb",
                dir=target.parent,
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(target)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def save_positions(
        self,
        state: RuntimeState,
        positions: list[ContractPosition],
        *,
        expected_sequence: int | None = None,
        expected_checksum: str | None = None,
    ) -> RuntimeStateRecord:
        state.positions = [asdict(position) for position in positions]
        return self.save(
            state,
            expected_sequence=expected_sequence,
            expected_checksum=expected_checksum,
        )

    def positions_from_state(self, state: RuntimeState) -> list[ContractPosition]:
        return [ContractPosition(**item) for item in state.positions]

    @staticmethod
    def can_clear_kill_switch(state: RuntimeState) -> bool:
        return state.kill_switch and state.reconciled and state.metadata_verified

    @staticmethod
    def _checksum(schema_version: int, sequence: int, state: dict) -> str:
        payload = json.dumps(
            {"schema_version": schema_version, "sequence": sequence, "state": state},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(payload).hexdigest()
