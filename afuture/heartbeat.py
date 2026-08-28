"""Rate-limited durable heartbeat for live and Shadow runtime supervision."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from .alerts import AlertManager
from .durable_json import (
    DurableJsonError,
    atomic_replace_regular,
    canonical_json_bytes,
    read_regular_json,
)

HEARTBEAT_KIND = "afuture.runtime.heartbeat"
HEARTBEAT_SCHEMA_VERSION = 1
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SECRET_KEY_FRAGMENTS = (
    "password",
    "auth_code",
    "authcode",
    "credential",
    "secret",
    "webhook",
    "access_token",
    "refresh_token",
    "user_id",
    "username",
)
_RESERVED_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "process_uuid",
        "pid",
        "process_start_utc",
        "heartbeat_utc",
        "monotonic_elapsed_seconds",
        "process_status",
        "clean_shutdown",
        "final_state_checksum",
        "checksum",
    }
)


class HeartbeatIntegrityError(RuntimeError):
    """Heartbeat content or its local filesystem path cannot be trusted."""


def _reject_secrets(value: object, *, path: str = "heartbeat") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).lower()
            if any(fragment in key for fragment in _SECRET_KEY_FRAGMENTS):
                raise HeartbeatIntegrityError(
                    f"heartbeat secret-like field is forbidden: {path}.{raw_key}"
                )
            _reject_secrets(item, path=f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secrets(item, path=f"{path}[{index}]")


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise HeartbeatIntegrityError("heartbeat clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _checksum(payload: Mapping[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "checksum"}
    return sha256(canonical_json_bytes(unsigned)).hexdigest()


def _validate_payload(raw: dict[str, Any]) -> dict[str, object]:
    _reject_secrets(raw)
    if raw.get("kind") != HEARTBEAT_KIND or raw.get("schema_version") != HEARTBEAT_SCHEMA_VERSION:
        raise HeartbeatIntegrityError("heartbeat kind or schema is unsupported")
    process_uuid = raw.get("process_uuid")
    try:
        UUID(str(process_uuid))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HeartbeatIntegrityError("heartbeat process UUID is invalid") from exc
    if type(raw.get("pid")) is not int or int(raw["pid"]) <= 0:
        raise HeartbeatIntegrityError("heartbeat PID is invalid")
    for name in ("process_start_utc", "heartbeat_utc"):
        value = raw.get(name)
        if not isinstance(value, str):
            raise HeartbeatIntegrityError(f"heartbeat {name} is invalid")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise HeartbeatIntegrityError(f"heartbeat {name} is invalid") from exc
        if parsed.tzinfo is None:
            raise HeartbeatIntegrityError(f"heartbeat {name} must be timezone-aware")
    elapsed = raw.get("monotonic_elapsed_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or float(elapsed) < 0:
        raise HeartbeatIntegrityError("heartbeat monotonic elapsed is invalid")
    if raw.get("process_status") not in {"running", "stopped"}:
        raise HeartbeatIntegrityError("heartbeat process status is invalid")
    if not isinstance(raw.get("clean_shutdown"), bool):
        raise HeartbeatIntegrityError("heartbeat clean shutdown marker is invalid")
    final_state_checksum = raw.get("final_state_checksum")
    if not isinstance(final_state_checksum, str) or (
        final_state_checksum and _HEX64.fullmatch(final_state_checksum) is None
    ):
        raise HeartbeatIntegrityError("heartbeat final state checksum is invalid")
    checksum = raw.get("checksum")
    if not isinstance(checksum, str) or _HEX64.fullmatch(checksum) is None:
        raise HeartbeatIntegrityError("heartbeat checksum is invalid")
    if checksum != _checksum(raw):
        raise HeartbeatIntegrityError("heartbeat checksum mismatch")
    return dict(raw)


def load_heartbeat(path: str | Path) -> dict[str, object]:
    try:
        raw = read_regular_json(path, label="heartbeat")
        return _validate_payload(raw)
    except HeartbeatIntegrityError:
        raise
    except (FileNotFoundError, DurableJsonError) as exc:
        raise HeartbeatIntegrityError(str(exc)) from exc


class HeartbeatWriter:
    """Write at most one durable heartbeat per configured interval from the main loop."""

    def __init__(
        self,
        path: str | Path,
        *,
        interval_seconds: float,
        process_uuid: str | None = None,
        process_start_utc: str | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        utc_clock: Callable[[], datetime] | None = None,
        alert_manager: AlertManager | None = None,
    ) -> None:
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)):
            raise ValueError("heartbeat interval must be numeric")
        if not 1.0 <= float(interval_seconds) <= 60.0:
            raise ValueError("heartbeat interval must be between 1 and 60 seconds")
        self.path = Path(path)
        self.interval_seconds = float(interval_seconds)
        self.process_uuid = str(process_uuid or uuid4())
        try:
            UUID(self.process_uuid)
        except ValueError as exc:
            raise ValueError("heartbeat process UUID is invalid") from exc
        self._monotonic_clock = monotonic_clock
        self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self._start_monotonic = float(self._monotonic_clock())
        self.process_start_utc = process_start_utc or _utc_iso(self._utc_clock())
        try:
            start = datetime.fromisoformat(self.process_start_utc)
        except ValueError as exc:
            raise ValueError("heartbeat process start UTC is invalid") from exc
        if start.tzinfo is None:
            raise ValueError("heartbeat process start UTC must be timezone-aware")
        self.alert_manager = alert_manager or AlertManager()
        self._last_write_monotonic: float | None = None

    def _payload(
        self,
        facts: Mapping[str, object],
        *,
        now_monotonic: float,
        process_status: str,
        clean: bool,
        final_state_checksum: str,
    ) -> dict[str, object]:
        if not isinstance(facts, Mapping):
            raise HeartbeatIntegrityError("heartbeat facts must be a mapping")
        if _RESERVED_FIELDS.intersection(facts):
            raise HeartbeatIntegrityError("heartbeat facts contain reserved fields")
        _reject_secrets(facts)
        if final_state_checksum and _HEX64.fullmatch(final_state_checksum) is None:
            raise HeartbeatIntegrityError("heartbeat final state checksum is invalid")
        payload: dict[str, object] = {
            "kind": HEARTBEAT_KIND,
            "schema_version": HEARTBEAT_SCHEMA_VERSION,
            "process_uuid": self.process_uuid,
            "pid": os.getpid(),
            "process_start_utc": self.process_start_utc,
            "heartbeat_utc": _utc_iso(self._utc_clock()),
            "monotonic_elapsed_seconds": max(0.0, now_monotonic - self._start_monotonic),
            **dict(facts),
            "process_status": process_status,
            "clean_shutdown": bool(clean),
            "final_state_checksum": final_state_checksum,
        }
        payload["checksum"] = _checksum(payload)
        _validate_payload(dict(payload))
        return payload

    def _write_payload(self, payload: Mapping[str, object], *, now_monotonic: float) -> bool:
        try:
            atomic_replace_regular(
                self.path,
                canonical_json_bytes(payload) + b"\n",
                label="heartbeat",
            )
        except DurableJsonError as exc:
            if "symlink" in str(exc).lower() or "regular file" in str(exc).lower():
                raise HeartbeatIntegrityError(str(exc)) from exc
            self.alert_manager.critical(
                "heartbeat write failed",
                {"error_category": type(exc).__name__},
            )
            return False
        self._last_write_monotonic = now_monotonic
        return True

    def write(self, facts: Mapping[str, object], *, force: bool = False) -> bool:
        now = float(self._monotonic_clock())
        if (
            not force
            and self._last_write_monotonic is not None
            and now - self._last_write_monotonic < self.interval_seconds
        ):
            return False
        payload = self._payload(
            facts,
            now_monotonic=now,
            process_status="running",
            clean=False,
            final_state_checksum="",
        )
        return self._write_payload(payload, now_monotonic=now)

    def write_stopped(
        self,
        facts: Mapping[str, object],
        *,
        final_state_checksum: str,
        clean: bool,
    ) -> bool:
        now = float(self._monotonic_clock())
        payload = self._payload(
            facts,
            now_monotonic=now,
            process_status="stopped",
            clean=clean,
            final_state_checksum=final_state_checksum,
        )
        return self._write_payload(payload, now_monotonic=now)
