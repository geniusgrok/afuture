"""Durable process-run receipts and fail-closed unclean-restart fencing."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .durable_json import (
    DurableJsonError,
    atomic_replace_regular,
    canonical_json_bytes,
    read_regular_json,
)
from .models import RuntimeMode
from .state import RuntimeState, StateIntegrityError, StateStore

PROCESS_RUN_KIND = "afuture.runtime.process-run"
PROCESS_RUN_SCHEMA_VERSION = 1
PROCESS_RUN_FILENAME = "process_run.json"
PROCESS_FENCE_EXIT_CODE = 75
_HEX64 = re.compile(r"[0-9a-f]{64}")
_ALLOWED_PHASES = frozenset(
    {
        "run_marker_written",
        "broker_constructed",
        "broker_ready_not_activated",
        "permit_consumed",
        "running_state_pending",
        "running",
        "shutdown_state_saved",
        "heartbeat_stopped_pending_receipt",
        "stopped",
        "restart_fenced",
    }
)
_CURRENT_PROCESS_UUID: str | None = None


class ProcessRunIntegrityError(RuntimeError):
    """The process-run chain is missing, corrupt, unsafe, or ambiguous."""


@dataclass(frozen=True)
class ProcessRunRecord:
    process_uuid: str
    phase: str
    deployment_digest: str
    runtime_identity_digest: str
    account_identity_digest: str
    start_state_checksum: str
    latest_state_checksum: str
    stop_state_checksum: str
    started_utc: str
    stopped_utc: str
    clean_shutdown: bool
    sequence: int
    parent_checksum: str | None
    checksum: str


@dataclass(frozen=True)
class RestartFenceResult:
    blocked: bool
    exit_code: int
    reason: str = ""
    process_uuid: str = ""


def current_process_uuid() -> str | None:
    return _CURRENT_PROCESS_UUID


def set_current_process_uuid(value: str | None) -> None:
    global _CURRENT_PROCESS_UUID
    if value is not None:
        try:
            UUID(value)
        except ValueError as exc:
            raise ProcessRunIntegrityError("process UUID is invalid") from exc
    _CURRENT_PROCESS_UUID = value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hex64(value: object, *, name: str, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ProcessRunIntegrityError(f"{name} must be a lowercase SHA-256")
    return value


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ProcessRunIntegrityError("process UUID is invalid")
    try:
        UUID(value)
    except ValueError as exc:
        raise ProcessRunIntegrityError("process UUID is invalid") from exc
    return value


def _timestamp(value: object, *, name: str, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str):
        raise ProcessRunIntegrityError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProcessRunIntegrityError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ProcessRunIntegrityError(f"{name} must be timezone-aware")
    return value


def _unsigned(record: ProcessRunRecord) -> dict[str, object]:
    payload = asdict(record)
    payload.pop("checksum")
    return {"kind": PROCESS_RUN_KIND, "schema_version": PROCESS_RUN_SCHEMA_VERSION, **payload}


def _checksum(unsigned: dict[str, object]) -> str:
    return sha256(canonical_json_bytes(unsigned)).hexdigest()


def _encode(record: ProcessRunRecord) -> bytes:
    unsigned = _unsigned(record)
    return canonical_json_bytes({**unsigned, "checksum": record.checksum}) + b"\n"


class ProcessRunStore:
    """Checksummed current/.prev chain. `.prev` is evidence, never recovery input."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(self.path.name + ".prev")

    def _decode(self, raw: dict[str, Any]) -> ProcessRunRecord:
        expected = {
            "kind",
            "schema_version",
            "process_uuid",
            "phase",
            "deployment_digest",
            "runtime_identity_digest",
            "account_identity_digest",
            "start_state_checksum",
            "latest_state_checksum",
            "stop_state_checksum",
            "started_utc",
            "stopped_utc",
            "clean_shutdown",
            "sequence",
            "parent_checksum",
            "checksum",
        }
        if set(raw) != expected or raw.get("kind") != PROCESS_RUN_KIND:
            raise ProcessRunIntegrityError("process-run envelope fields or kind are invalid")
        if raw.get("schema_version") != PROCESS_RUN_SCHEMA_VERSION:
            raise ProcessRunIntegrityError("process-run schema is unsupported")
        process_uuid = _uuid(raw.get("process_uuid"))
        phase = raw.get("phase")
        if not isinstance(phase, str) or phase not in _ALLOWED_PHASES:
            raise ProcessRunIntegrityError("process-run phase is invalid")
        deployment = _hex64(raw.get("deployment_digest"), name="deployment digest")
        runtime = _hex64(raw.get("runtime_identity_digest"), name="runtime identity digest")
        account = _hex64(raw.get("account_identity_digest"), name="account identity digest")
        start_state = _hex64(raw.get("start_state_checksum"), name="start state checksum")
        latest_state = _hex64(
            raw.get("latest_state_checksum"),
            name="latest state checksum",
            allow_empty=True,
        )
        stop_state = _hex64(
            raw.get("stop_state_checksum"),
            name="stop state checksum",
            allow_empty=True,
        )
        started = _timestamp(raw.get("started_utc"), name="process start UTC")
        stopped = _timestamp(raw.get("stopped_utc"), name="process stop UTC", allow_empty=True)
        clean = raw.get("clean_shutdown")
        if not isinstance(clean, bool):
            raise ProcessRunIntegrityError("process-run clean marker is invalid")
        if clean and (not stopped or not stop_state):
            raise ProcessRunIntegrityError("clean process-run receipt requires stop evidence")
        sequence = raw.get("sequence")
        if type(sequence) is not int or int(sequence) <= 0:
            raise ProcessRunIntegrityError("process-run sequence is invalid")
        parent = raw.get("parent_checksum")
        if sequence == 1:
            if parent is not None:
                raise ProcessRunIntegrityError("initial process-run parent is invalid")
        elif not isinstance(parent, str) or _HEX64.fullmatch(parent) is None:
            raise ProcessRunIntegrityError("process-run parent checksum is invalid")
        checksum = raw.get("checksum")
        if not isinstance(checksum, str) or _HEX64.fullmatch(checksum) is None:
            raise ProcessRunIntegrityError("process-run checksum is invalid")
        record = ProcessRunRecord(
            process_uuid=process_uuid,
            phase=phase,
            deployment_digest=deployment,
            runtime_identity_digest=runtime,
            account_identity_digest=account,
            start_state_checksum=start_state,
            latest_state_checksum=latest_state,
            stop_state_checksum=stop_state,
            started_utc=started,
            stopped_utc=stopped,
            clean_shutdown=clean,
            sequence=int(sequence),
            parent_checksum=parent,
            checksum=checksum,
        )
        if checksum != _checksum(_unsigned(record)):
            raise ProcessRunIntegrityError("process-run checksum mismatch")
        return record

    def _read(self, path: Path) -> ProcessRunRecord:
        try:
            return self._decode(read_regular_json(path, label="process-run"))
        except ProcessRunIntegrityError:
            raise
        except (FileNotFoundError, DurableJsonError) as exc:
            raise ProcessRunIntegrityError(str(exc)) from exc

    def load_record(self) -> ProcessRunRecord | None:
        if not self.path.exists() and not self.path.is_symlink():
            if self.previous_path.exists() or self.previous_path.is_symlink():
                raise ProcessRunIntegrityError("process-run current is missing while .prev exists")
            return None
        current = self._read(self.path)
        if not self.previous_path.exists() and not self.previous_path.is_symlink():
            if current.sequence != 1:
                raise ProcessRunIntegrityError("process-run .prev evidence is missing")
            return current
        previous = self._read(self.previous_path)
        if previous.sequence == current.sequence and previous.checksum == current.checksum:
            return current
        if previous.sequence + 1 != current.sequence or current.parent_checksum != previous.checksum:
            raise ProcessRunIntegrityError("process-run predecessor chain mismatch")
        return current

    def load_required(self) -> ProcessRunRecord:
        record = self.load_record()
        if record is None:
            raise ProcessRunIntegrityError("required process-run receipt is missing")
        return record

    def _replace(self, record: ProcessRunRecord, current: ProcessRunRecord | None) -> ProcessRunRecord:
        if current is not None:
            try:
                atomic_replace_regular(
                    self.previous_path,
                    _encode(current),
                    label="process-run .prev",
                )
            except DurableJsonError as exc:
                raise ProcessRunIntegrityError(str(exc)) from exc
        try:
            atomic_replace_regular(self.path, _encode(record), label="process-run")
        except DurableJsonError as exc:
            raise ProcessRunIntegrityError(str(exc)) from exc
        reloaded = self.load_required()
        if reloaded != record:
            raise ProcessRunIntegrityError("process-run changed during durable save")
        return reloaded

    def _transition(
        self,
        current: ProcessRunRecord,
        *,
        phase: str,
        latest_state_checksum: str | None = None,
        stop_state_checksum: str = "",
        stopped_utc: str = "",
        clean_shutdown: bool = False,
    ) -> ProcessRunRecord:
        if phase not in _ALLOWED_PHASES:
            raise ProcessRunIntegrityError("process-run phase is invalid")
        latest = current.latest_state_checksum
        if latest_state_checksum is not None:
            latest = _hex64(latest_state_checksum, name="latest state checksum")
        stop = _hex64(stop_state_checksum, name="stop state checksum", allow_empty=True)
        stopped = _timestamp(stopped_utc, name="process stop UTC", allow_empty=True)
        if clean_shutdown and (not stop or not stopped):
            raise ProcessRunIntegrityError("clean process-run transition requires stop evidence")
        candidate = ProcessRunRecord(
            process_uuid=current.process_uuid,
            phase=phase,
            deployment_digest=current.deployment_digest,
            runtime_identity_digest=current.runtime_identity_digest,
            account_identity_digest=current.account_identity_digest,
            start_state_checksum=current.start_state_checksum,
            latest_state_checksum=latest,
            stop_state_checksum=stop,
            started_utc=current.started_utc,
            stopped_utc=stopped,
            clean_shutdown=clean_shutdown,
            sequence=current.sequence + 1,
            parent_checksum=current.checksum,
            checksum="",
        )
        candidate = replace(candidate, checksum=_checksum(_unsigned(candidate)))
        return self._replace(candidate, current)

    def begin(
        self,
        *,
        deployment_digest: str,
        runtime_identity_digest: str,
        account_identity_digest: str,
        start_state_checksum: str,
        process_uuid: str | None = None,
        _allow_unclean_parent: bool = False,
    ) -> ProcessRunRecord:
        current = self.load_record()
        if current is not None and not current.clean_shutdown and not _allow_unclean_parent:
            raise ProcessRunIntegrityError("previous process-run has no clean shutdown receipt")
        sequence = 1 if current is None else current.sequence + 1
        parent = None if current is None else current.checksum
        candidate = ProcessRunRecord(
            process_uuid=_uuid(str(process_uuid or uuid4())),
            phase="run_marker_written",
            deployment_digest=_hex64(deployment_digest, name="deployment digest"),
            runtime_identity_digest=_hex64(runtime_identity_digest, name="runtime identity digest"),
            account_identity_digest=_hex64(account_identity_digest, name="account identity digest"),
            start_state_checksum=_hex64(start_state_checksum, name="start state checksum"),
            latest_state_checksum=_hex64(start_state_checksum, name="start state checksum"),
            stop_state_checksum="",
            started_utc=_utc_now(),
            stopped_utc="",
            clean_shutdown=False,
            sequence=sequence,
            parent_checksum=parent,
            checksum="",
        )
        candidate = replace(candidate, checksum=_checksum(_unsigned(candidate)))
        return self._replace(candidate, current)

    def mark_phase(
        self,
        process_uuid: str,
        phase: str,
        *,
        state_checksum: str | None = None,
    ) -> ProcessRunRecord:
        current = self.load_required()
        if current.process_uuid != _uuid(process_uuid) or current.clean_shutdown:
            raise ProcessRunIntegrityError("process-run phase CAS does not match current process")
        return self._transition(current, phase=phase, latest_state_checksum=state_checksum)

    def finish_clean(
        self,
        process_uuid: str,
        *,
        stop_state_checksum: str,
        stopped_utc: str | None = None,
        phase: str = "stopped",
    ) -> ProcessRunRecord:
        current = self.load_required()
        if current.process_uuid != _uuid(process_uuid) or current.clean_shutdown:
            raise ProcessRunIntegrityError("process-run clean CAS does not match current process")
        stop = _hex64(stop_state_checksum, name="stop state checksum")
        return self._transition(
            current,
            phase=phase,
            latest_state_checksum=stop,
            stop_state_checksum=stop,
            stopped_utc=stopped_utc or _utc_now(),
            clean_shutdown=True,
        )


def _halt_state_exact(state_store: StateStore, *, reason: str):
    current = state_store.load_record()
    state = RuntimeState() if current is None else current.state
    halted = replace(
        state,
        kill_switch=True,
        kill_reason=reason,
        runtime_mode=RuntimeMode.HALTED.value,
        reconciled=False,
    )
    try:
        return state_store.save(
            halted,
            expected_sequence=0 if current is None else current.sequence,
            expected_checksum="" if current is None else current.checksum,
        )
    except StateIntegrityError as exc:
        raise ProcessRunIntegrityError("runtime state changed while applying restart fence") from exc


def _invalidate_permit(runtime_dir: Path, reason: str) -> None:
    permit_path = runtime_dir / "stress90_activation_permit.json"
    previous = permit_path.with_name(permit_path.name + ".prev")
    if not permit_path.exists() and not permit_path.is_symlink() and not previous.exists() and not previous.is_symlink():
        return
    from .stress90_activation_permit import Stress90ActivationPermitStore

    Stress90ActivationPermitStore(permit_path).invalidate(reason)


def apply_unclean_restart_fence(
    *,
    process_store: ProcessRunStore,
    state_store: StateStore,
    runtime_dir: str | Path,
    deployment_digest: str,
    runtime_identity_digest: str,
    account_identity_digest: str,
) -> RestartFenceResult:
    """Halt exact local truth and invalidate technical authority after an unclean run."""

    previous = process_store.load_record()
    if previous is None or previous.clean_shutdown:
        return RestartFenceResult(False, 0)
    reason = f"unclean restart fence: prior process {previous.process_uuid} stopped in {previous.phase}"
    fenced_state = _halt_state_exact(state_store, reason=reason)
    _invalidate_permit(Path(runtime_dir), reason)
    handler = process_store.begin(
        deployment_digest=deployment_digest,
        runtime_identity_digest=runtime_identity_digest,
        account_identity_digest=account_identity_digest,
        start_state_checksum=fenced_state.checksum,
        _allow_unclean_parent=True,
    )
    finished = process_store.finish_clean(
        handler.process_uuid,
        stop_state_checksum=fenced_state.checksum,
        phase="restart_fenced",
    )
    return RestartFenceResult(True, PROCESS_FENCE_EXIT_CODE, reason, finished.process_uuid)
