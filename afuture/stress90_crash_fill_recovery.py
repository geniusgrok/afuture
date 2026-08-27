"""Durable HALTED coordinator for authorized Stress-90 crash-fill adoption."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
import struct
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from .broker.ctp_session_query import (
    CtpSessionActivityEvidence,
    CtpSessionOrder,
    CtpSessionTrade,
    build_ctp_session_activity_evidence,
)
from .models import ContractPosition, Offset, OrderSide, OrderType, RuntimeMode
from .state import SCHEMA_VERSION as GENERIC_STATE_SCHEMA_VERSION
from .state import RuntimeState, RuntimeStateRecord, StateStore

STRESS90_CRASH_FILL_RECOVERY_STATE_KEY = "stress90_crash_fill_recovery"
STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION = "I_CONFIRM_AUTHORIZED_STRESS90_CRASH_FILL_RECOVERY"

_KIND = "afuture.stress90-crash-fill-recovery"
_SCHEMA = 1
_SHA = re.compile(r"[0-9a-f]{64}")
_STATUSES = {"prepared", "committed"}
_LINEAGE_MARKER = b'{"kind":"afuture.stress90-crash-fill-recovery-lineage","schema_version":1}\n'
_F_OFD_SETLKW = getattr(fcntl, "F_OFD_SETLKW", 38)


class Stress90CrashFillRecoveryError(RuntimeError):
    """Crash-fill recovery evidence cannot be trusted or advanced."""


@dataclass(frozen=True)
class Stress90CrashFillRecoveryAuthority:
    account_identity_digest: str
    account_epoch: str
    canonical_runtime: str
    runtime_identity_digest: str
    account_binding_payload_digest: str
    account_binding_revision: int
    account_binding_last_operation_id: str
    account_binding_receipt_digest: str
    registry_sequence: int
    registry_checksum: str
    policy_state_sequence: int
    policy_state_checksum: str


@dataclass(frozen=True)
class Stress90CrashFillRecoveryCheckpoint:
    transaction_id: str
    operation_nonce: str
    operator_reason: str
    status: str
    authority: Stress90CrashFillRecoveryAuthority
    trading_day: str
    session_evidence: CtpSessionActivityEvidence
    session_evidence_sequence: int
    session_evidence_checksum: str
    session_ownership_digest: str
    generic_source_sequence: int
    generic_source_checksum: str
    generic_target: RuntimeState
    generic_target_sequence: int
    generic_target_checksum: str
    source_positions_digest: str
    target_positions_digest: str
    adopted_fill_ids: tuple[str, ...]


@dataclass(frozen=True)
class Stress90CrashFillRecoveryOperation:
    operation_nonce: str
    request_digest: str


@dataclass(frozen=True)
class Stress90CrashFillRecoveryRecord:
    checkpoint: Stress90CrashFillRecoveryCheckpoint
    operation_history: tuple[Stress90CrashFillRecoveryOperation, ...]
    sequence: int
    parent_checksum: str | None
    checksum: str


@dataclass
class _ExclusiveLock:
    surviving_visible_lock: bool
    visible_descriptor: int | None = None


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Stress90CrashFillRecoveryError(
            "Stress-90 crash-fill recovery is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def stress90_crash_fill_positions_digest(
    positions: list[ContractPosition] | tuple[ContractPosition, ...],
) -> str:
    """Canonical position identity used by the recovery coordinator."""

    normalized: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for position in sorted(positions, key=lambda item: (item.exchange, item.symbol)):
        if not isinstance(position, ContractPosition):
            raise Stress90CrashFillRecoveryError("recovery position evidence is invalid")
        try:
            position.validate()
        except ValueError as exc:
            raise Stress90CrashFillRecoveryError("recovery position evidence is invalid") from exc
        identity = (position.symbol, position.exchange)
        if identity in seen:
            raise Stress90CrashFillRecoveryError("recovery position evidence is duplicated")
        seen.add(identity)
        normalized.append(asdict(position))
    return _digest(normalized)


def _sha(value: object, name: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise Stress90CrashFillRecoveryError(f"{name} must be lowercase SHA-256")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise Stress90CrashFillRecoveryError(f"{name} must be a positive integer")
    return value


def _reason(value: object) -> str:
    if type(value) is not str or not value.strip() or value.strip() != value:
        raise Stress90CrashFillRecoveryError("recovery operator reason is required")
    return value


def _session_order_payload(order: CtpSessionOrder) -> dict[str, object]:
    return {
        "broker_id": order.broker_id,
        "investor_id": order.investor_id,
        "invest_unit_id": order.invest_unit_id,
        "trading_day": order.trading_day,
        "instrument_id": order.instrument_id,
        "exchange_id": order.exchange_id,
        "order_sys_id": order.order_sys_id,
        "front_id": order.front_id,
        "session_id": order.session_id,
        "order_ref": order.order_ref,
        "order_status": order.order_status,
        "side": order.side.value,
        "offset": order.offset.value,
        "volume": order.volume,
        "volume_traded": order.volume_traded,
        "volume_total": order.volume_total,
        "price": order.price,
        "order_type": order.order_type.value,
    }


def _session_trade_payload(trade: CtpSessionTrade) -> dict[str, object]:
    return {
        "broker_id": trade.broker_id,
        "investor_id": trade.investor_id,
        "invest_unit_id": trade.invest_unit_id,
        "trading_day": trade.trading_day,
        "instrument_id": trade.instrument_id,
        "exchange_id": trade.exchange_id,
        "trade_id": trade.trade_id,
        "order_sys_id": trade.order_sys_id,
        "order_ref": trade.order_ref,
        "side": trade.side.value,
        "offset": trade.offset.value,
        "volume": trade.volume,
        "price": trade.price,
        "timestamp": trade.timestamp.isoformat(),
    }


def _session_payload(evidence: CtpSessionActivityEvidence) -> dict[str, object]:
    return {
        "account_identity_digest": evidence.account_identity_digest,
        "trading_day": evidence.trading_day,
        "order_request_id": evidence.order_request_id,
        "trade_request_id": evidence.trade_request_id,
        "orders": [_session_order_payload(item) for item in evidence.orders],
        "trades": [_session_trade_payload(item) for item in evidence.trades],
        "critical_generation": evidence.critical_generation,
        "query_ingress_generation": evidence.query_ingress_generation,
        "orders_digest": evidence.orders_digest,
        "trades_digest": evidence.trades_digest,
        "evidence_digest": evidence.evidence_digest,
    }


def _decode_session(raw: object) -> CtpSessionActivityEvidence:
    fields = {
        "account_identity_digest",
        "trading_day",
        "order_request_id",
        "trade_request_id",
        "orders",
        "trades",
        "critical_generation",
        "query_ingress_generation",
        "orders_digest",
        "trades_digest",
        "evidence_digest",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise Stress90CrashFillRecoveryError("recovery session evidence fields are invalid")
    orders_raw = raw["orders"]
    trades_raw = raw["trades"]
    if not isinstance(orders_raw, list) or not isinstance(trades_raw, list):
        raise Stress90CrashFillRecoveryError("recovery session evidence rows are invalid")
    try:
        orders = tuple(
            CtpSessionOrder(
                **{
                    **item,
                    "side": OrderSide(item["side"]),
                    "offset": Offset(item["offset"]),
                    "order_type": OrderType(item["order_type"]),
                }
            )
            for item in orders_raw
            if isinstance(item, dict)
        )
        trades = tuple(
            CtpSessionTrade(
                **{
                    **item,
                    "side": OrderSide(item["side"]),
                    "offset": Offset(item["offset"]),
                    "timestamp": datetime.fromisoformat(item["timestamp"]),
                }
            )
            for item in trades_raw
            if isinstance(item, dict)
        )
        if len(orders) != len(orders_raw) or len(trades) != len(trades_raw):
            raise TypeError("session evidence row is not an object")
        evidence = build_ctp_session_activity_evidence(
            account_identity_digest=raw["account_identity_digest"],
            trading_day=raw["trading_day"],
            order_request_id=raw["order_request_id"],
            trade_request_id=raw["trade_request_id"],
            orders=orders,
            trades=trades,
            critical_generation=raw["critical_generation"],
            query_ingress_generation=raw["query_ingress_generation"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Stress90CrashFillRecoveryError("recovery session evidence is invalid") from exc
    if _session_payload(evidence) != dict(raw):
        raise Stress90CrashFillRecoveryError("recovery session evidence digest mismatch")
    return evidence


def _authority_payload(authority: Stress90CrashFillRecoveryAuthority) -> dict[str, object]:
    payload = asdict(authority)
    for name in (
        "account_identity_digest",
        "account_epoch",
        "runtime_identity_digest",
        "account_binding_payload_digest",
        "account_binding_last_operation_id",
        "account_binding_receipt_digest",
        "registry_checksum",
        "policy_state_checksum",
    ):
        _sha(payload[name], name.replace("_", " "))
    _positive_int(authority.account_binding_revision, "account binding revision")
    _positive_int(authority.registry_sequence, "registry sequence")
    _positive_int(authority.policy_state_sequence, "policy state sequence")
    if (
        type(authority.canonical_runtime) is not str
        or not Path(authority.canonical_runtime).is_absolute()
        or os.path.realpath(authority.canonical_runtime) != authority.canonical_runtime
    ):
        raise Stress90CrashFillRecoveryError("recovery canonical runtime is invalid")
    return payload


def _without_recovery_marker(state: RuntimeState) -> dict[str, object]:
    payload = asdict(state)
    states = dict(payload["strategy_states"])
    states.pop(STRESS90_CRASH_FILL_RECOVERY_STATE_KEY, None)
    payload["strategy_states"] = states
    return payload


def _checkpoint_identity_payload(
    *,
    operation_nonce: str,
    operator_reason: str,
    authority: Stress90CrashFillRecoveryAuthority,
    trading_day: str,
    session_evidence: CtpSessionActivityEvidence,
    session_evidence_sequence: int,
    session_evidence_checksum: str,
    session_ownership_digest: str,
    generic_source_sequence: int,
    generic_source_checksum: str,
    generic_target: RuntimeState,
    source_positions_digest: str,
    target_positions_digest: str,
    adopted_fill_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "operation_nonce": operation_nonce,
        "operator_reason": operator_reason,
        "authority": _authority_payload(authority),
        "trading_day": trading_day,
        "session_evidence": _session_payload(session_evidence),
        "session_evidence_sequence": session_evidence_sequence,
        "session_evidence_checksum": session_evidence_checksum,
        "session_ownership_digest": session_ownership_digest,
        "generic_source_sequence": generic_source_sequence,
        "generic_source_checksum": generic_source_checksum,
        "generic_target_without_recovery_marker": _without_recovery_marker(generic_target),
        "source_positions_digest": source_positions_digest,
        "target_positions_digest": target_positions_digest,
        "adopted_fill_ids": list(adopted_fill_ids),
    }


def build_stress90_crash_fill_recovery_checkpoint(
    *,
    operation_nonce: str,
    operator_reason: str,
    authority: Stress90CrashFillRecoveryAuthority,
    trading_day: str,
    session_evidence: CtpSessionActivityEvidence,
    session_evidence_sequence: int,
    session_evidence_checksum: str,
    session_ownership_digest: str,
    generic_source: RuntimeStateRecord,
    generic_target: RuntimeState,
    source_positions_digest: str,
    target_positions_digest: str,
    adopted_fill_ids: tuple[str, ...],
) -> Stress90CrashFillRecoveryCheckpoint:
    """Build one immutable prepared request and bind its proof marker into target state."""

    nonce = _sha(operation_nonce, "recovery operation nonce")
    reason = _reason(operator_reason)
    if generic_source.legacy:
        raise Stress90CrashFillRecoveryError("legacy generic state cannot be recovered")
    if generic_target.runtime_mode != RuntimeMode.HALTED.value or not generic_target.kill_switch:
        raise Stress90CrashFillRecoveryError("recovery target must preserve HALTED kill switch")
    if generic_target.positions == generic_source.state.positions and tuple(
        generic_target.recent_trade_ids
    ) == tuple(generic_source.state.recent_trade_ids):
        raise Stress90CrashFillRecoveryError("no unpersisted crash fills require recovery")
    if session_evidence.account_identity_digest != authority.account_identity_digest:
        raise Stress90CrashFillRecoveryError("session/account recovery identity mismatch")
    if session_evidence.trading_day != trading_day:
        raise Stress90CrashFillRecoveryError("session recovery trading day mismatch")
    _positive_int(session_evidence_sequence, "session evidence sequence")
    _sha(session_evidence_checksum, "session evidence checksum")
    _sha(session_ownership_digest, "session ownership digest")
    _sha(source_positions_digest, "source positions digest")
    _sha(target_positions_digest, "target positions digest")
    normalized_ids = tuple(adopted_fill_ids)
    if (
        not normalized_ids
        or len(set(normalized_ids)) != len(normalized_ids)
        or any(type(item) is not str or not item for item in normalized_ids)
    ):
        raise Stress90CrashFillRecoveryError("adopted fill identities are invalid")
    identity = _checkpoint_identity_payload(
        operation_nonce=nonce,
        operator_reason=reason,
        authority=authority,
        trading_day=trading_day,
        session_evidence=session_evidence,
        session_evidence_sequence=session_evidence_sequence,
        session_evidence_checksum=session_evidence_checksum,
        session_ownership_digest=session_ownership_digest,
        generic_source_sequence=generic_source.sequence,
        generic_source_checksum=generic_source.checksum,
        generic_target=generic_target,
        source_positions_digest=source_positions_digest,
        target_positions_digest=target_positions_digest,
        adopted_fill_ids=normalized_ids,
    )
    transaction_id = _digest(identity)
    strategy_states = dict(generic_target.strategy_states)
    strategy_states[STRESS90_CRASH_FILL_RECOVERY_STATE_KEY] = {
        "transaction_id": transaction_id,
    }
    marked_target = replace(generic_target, strategy_states=strategy_states)
    StateStore._state_from_payload(asdict(marked_target))
    target_sequence = generic_source.sequence + 1
    target_checksum = StateStore._checksum(
        GENERIC_STATE_SCHEMA_VERSION,
        target_sequence,
        asdict(marked_target),
    )
    return Stress90CrashFillRecoveryCheckpoint(
        transaction_id=transaction_id,
        operation_nonce=nonce,
        operator_reason=reason,
        status="prepared",
        authority=authority,
        trading_day=trading_day,
        session_evidence=session_evidence,
        session_evidence_sequence=session_evidence_sequence,
        session_evidence_checksum=session_evidence_checksum,
        session_ownership_digest=session_ownership_digest,
        generic_source_sequence=generic_source.sequence,
        generic_source_checksum=generic_source.checksum,
        generic_target=marked_target,
        generic_target_sequence=target_sequence,
        generic_target_checksum=target_checksum,
        source_positions_digest=source_positions_digest,
        target_positions_digest=target_positions_digest,
        adopted_fill_ids=normalized_ids,
    )


def _checkpoint_payload(checkpoint: Stress90CrashFillRecoveryCheckpoint) -> dict[str, object]:
    return {
        "transaction_id": checkpoint.transaction_id,
        "operation_nonce": checkpoint.operation_nonce,
        "operator_reason": checkpoint.operator_reason,
        "status": checkpoint.status,
        "authority": _authority_payload(checkpoint.authority),
        "trading_day": checkpoint.trading_day,
        "session_evidence": _session_payload(checkpoint.session_evidence),
        "session_evidence_sequence": checkpoint.session_evidence_sequence,
        "session_evidence_checksum": checkpoint.session_evidence_checksum,
        "session_ownership_digest": checkpoint.session_ownership_digest,
        "generic_source_sequence": checkpoint.generic_source_sequence,
        "generic_source_checksum": checkpoint.generic_source_checksum,
        "generic_target": asdict(checkpoint.generic_target),
        "generic_target_sequence": checkpoint.generic_target_sequence,
        "generic_target_checksum": checkpoint.generic_target_checksum,
        "source_positions_digest": checkpoint.source_positions_digest,
        "target_positions_digest": checkpoint.target_positions_digest,
        "adopted_fill_ids": list(checkpoint.adopted_fill_ids),
    }


def _decode_authority(raw: object) -> Stress90CrashFillRecoveryAuthority:
    fields = set(Stress90CrashFillRecoveryAuthority.__dataclass_fields__)
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise Stress90CrashFillRecoveryError("recovery authority fields are invalid")
    try:
        authority = Stress90CrashFillRecoveryAuthority(**dict(raw))
    except TypeError as exc:
        raise Stress90CrashFillRecoveryError("recovery authority is invalid") from exc
    _authority_payload(authority)
    return authority


def _decode_checkpoint(raw: object) -> Stress90CrashFillRecoveryCheckpoint:
    fields = {
        "transaction_id",
        "operation_nonce",
        "operator_reason",
        "status",
        "authority",
        "trading_day",
        "session_evidence",
        "session_evidence_sequence",
        "session_evidence_checksum",
        "session_ownership_digest",
        "generic_source_sequence",
        "generic_source_checksum",
        "generic_target",
        "generic_target_sequence",
        "generic_target_checksum",
        "source_positions_digest",
        "target_positions_digest",
        "adopted_fill_ids",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise Stress90CrashFillRecoveryError("recovery checkpoint fields are invalid")
    status = raw["status"]
    if status not in _STATUSES:
        raise Stress90CrashFillRecoveryError("recovery checkpoint status is invalid")
    target_raw = raw["generic_target"]
    if not isinstance(target_raw, dict):
        raise Stress90CrashFillRecoveryError("recovery generic target is invalid")
    target = StateStore._state_from_payload(target_raw)
    adopted = raw["adopted_fill_ids"]
    if not isinstance(adopted, list):
        raise Stress90CrashFillRecoveryError("recovery adopted fill identities are invalid")
    try:
        checkpoint = Stress90CrashFillRecoveryCheckpoint(
            transaction_id=_sha(raw["transaction_id"], "recovery transaction identity"),
            operation_nonce=_sha(raw["operation_nonce"], "recovery operation nonce"),
            operator_reason=_reason(raw["operator_reason"]),
            status=str(status),
            authority=_decode_authority(raw["authority"]),
            trading_day=str(raw["trading_day"]),
            session_evidence=_decode_session(raw["session_evidence"]),
            session_evidence_sequence=_positive_int(
                raw["session_evidence_sequence"], "session evidence sequence"
            ),
            session_evidence_checksum=_sha(
                raw["session_evidence_checksum"], "session evidence checksum"
            ),
            session_ownership_digest=_sha(
                raw["session_ownership_digest"], "session ownership digest"
            ),
            generic_source_sequence=_positive_int(
                raw["generic_source_sequence"], "generic source sequence"
            ),
            generic_source_checksum=_sha(raw["generic_source_checksum"], "generic source checksum"),
            generic_target=target,
            generic_target_sequence=_positive_int(
                raw["generic_target_sequence"], "generic target sequence"
            ),
            generic_target_checksum=_sha(raw["generic_target_checksum"], "generic target checksum"),
            source_positions_digest=_sha(raw["source_positions_digest"], "source positions digest"),
            target_positions_digest=_sha(raw["target_positions_digest"], "target positions digest"),
            adopted_fill_ids=tuple(adopted),
        )
    except (TypeError, ValueError) as exc:
        raise Stress90CrashFillRecoveryError("recovery checkpoint is invalid") from exc
    if checkpoint.generic_target_sequence != checkpoint.generic_source_sequence + 1:
        raise Stress90CrashFillRecoveryError("recovery generic target sequence is invalid")
    if checkpoint.generic_target.runtime_mode != RuntimeMode.HALTED.value or not (
        checkpoint.generic_target.kill_switch
    ):
        raise Stress90CrashFillRecoveryError("recovery target must preserve HALTED kill switch")
    marker = checkpoint.generic_target.strategy_states.get(STRESS90_CRASH_FILL_RECOVERY_STATE_KEY)
    if not isinstance(marker, dict) or marker != {"transaction_id": checkpoint.transaction_id}:
        raise Stress90CrashFillRecoveryError("recovery generic proof marker is invalid")
    expected_target = StateStore._checksum(
        GENERIC_STATE_SCHEMA_VERSION,
        checkpoint.generic_target_sequence,
        asdict(checkpoint.generic_target),
    )
    if checkpoint.generic_target_checksum != expected_target:
        raise Stress90CrashFillRecoveryError("recovery generic target checksum mismatch")
    identity = _checkpoint_identity_payload(
        operation_nonce=checkpoint.operation_nonce,
        operator_reason=checkpoint.operator_reason,
        authority=checkpoint.authority,
        trading_day=checkpoint.trading_day,
        session_evidence=checkpoint.session_evidence,
        session_evidence_sequence=checkpoint.session_evidence_sequence,
        session_evidence_checksum=checkpoint.session_evidence_checksum,
        session_ownership_digest=checkpoint.session_ownership_digest,
        generic_source_sequence=checkpoint.generic_source_sequence,
        generic_source_checksum=checkpoint.generic_source_checksum,
        generic_target=checkpoint.generic_target,
        source_positions_digest=checkpoint.source_positions_digest,
        target_positions_digest=checkpoint.target_positions_digest,
        adopted_fill_ids=checkpoint.adopted_fill_ids,
    )
    if checkpoint.transaction_id != _digest(identity):
        raise Stress90CrashFillRecoveryError("recovery transaction identity mismatch")
    return checkpoint


def _operation_payload(operation: Stress90CrashFillRecoveryOperation) -> dict[str, str]:
    return {
        "operation_nonce": _sha(operation.operation_nonce, "recovery operation nonce"),
        "request_digest": _sha(operation.request_digest, "recovery request digest"),
    }


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stress90CrashFillRecoveryError(f"duplicate recovery JSON key: {key}")
        result[key] = value
    return result


class Stress90CrashFillRecoveryStore:
    """Checksummed OFD/CAS coordinator; `.prev` is never a recovery input."""

    def __init__(self, path: str | Path) -> None:
        raw = os.fspath(path)
        if type(raw) is not str or not Path(raw).is_absolute():
            raise Stress90CrashFillRecoveryError("recovery checkpoint path must be absolute")
        canonical_parent = Path(os.path.realpath(os.path.dirname(raw)))
        self.path = canonical_parent / os.path.basename(raw)
        self.previous_path = self.path.with_name(self.path.name + ".prev")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lineage_path = self.path.with_name(self.path.name + ".lineage")

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            return path.exists() or path.is_symlink()
        except OSError as exc:
            raise Stress90CrashFillRecoveryError("recovery checkpoint path is invalid") from exc

    @staticmethod
    def _read_bytes(path: Path, label: str) -> bytes:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, "not a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError as exc:
            raise Stress90CrashFillRecoveryError(
                f"{label} crash-fill recovery evidence cannot be read"
            ) from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    raise Stress90CrashFillRecoveryError(
                        f"{label} crash-fill recovery evidence close failed"
                    ) from exc

    @staticmethod
    def _acquire_kernel_lock(path: Path) -> int:
        descriptor = os.open("/dev/null", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        identity = sha256(f"stress90-crash-fill-recovery:{path}".encode()).digest()
        offset = int.from_bytes(identity[:8], "big") % ((1 << 63) - 1)
        lock = struct.pack("hhqqi4x", fcntl.F_WRLCK, os.SEEK_SET, offset, 1, 0)
        try:
            fcntl.fcntl(descriptor, _F_OFD_SETLKW, lock)
        except OSError as exc:
            os.close(descriptor)
            raise Stress90CrashFillRecoveryError("crash-fill recovery kernel lock failed") from exc
        return descriptor

    @contextmanager
    def _exclusive_lock(self) -> Iterator[_ExclusiveLock]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        kernel = self._acquire_kernel_lock(self.path)
        lock = _ExclusiveLock(surviving_visible_lock=self._exists(self.lock_path))
        body_failed = False
        try:
            if lock.surviving_visible_lock or any(
                self._exists(path) for path in (self.path, self.previous_path, self.lineage_path)
            ):
                self._ensure_visible_lock_unlocked(lock)
            yield lock
        except BaseException:
            body_failed = True
            raise
        finally:
            cleanup_error: OSError | None = None
            if lock.visible_descriptor is not None:
                try:
                    os.close(lock.visible_descriptor)
                except OSError as exc:
                    cleanup_error = exc
            try:
                os.close(kernel)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
            if cleanup_error is not None and not body_failed:
                raise Stress90CrashFillRecoveryError(
                    "crash-fill recovery lock cleanup failed"
                ) from cleanup_error

    def _ensure_visible_lock_unlocked(self, lock: _ExclusiveLock) -> None:
        if lock.visible_descriptor is not None:
            return
        descriptor: int | None = None
        created = False
        try:
            flags = (
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                descriptor = os.open(self.lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                descriptor = os.open(self.lock_path, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, "recovery visible lock is not regular")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            visible = os.stat(self.lock_path, follow_symlinks=False)
            if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError(errno.ESTALE, "recovery visible lock changed")
            if created:
                os.fsync(descriptor)
                self._fsync_parent(self.lock_path)
            lock.visible_descriptor = descriptor
            descriptor = None
        except OSError as exc:
            raise Stress90CrashFillRecoveryError("crash-fill recovery lock failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _claim_initial_visible_lock_unlocked(self, lock: _ExclusiveLock) -> None:
        if lock.visible_descriptor is not None or lock.surviving_visible_lock:
            raise Stress90CrashFillRecoveryError(
                "crash-fill recovery initialization has surviving lock evidence"
            )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, "recovery initial lock is not regular")
            os.fsync(descriptor)
            self._fsync_parent(self.lock_path)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            visible = os.stat(self.lock_path, follow_symlinks=False)
            if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError(errno.ESTALE, "recovery initial lock changed")
            lock.visible_descriptor = descriptor
            descriptor = None
        except OSError as exc:
            raise Stress90CrashFillRecoveryError(
                "crash-fill recovery initialization lock claim failed"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        body_failed = False
        try:
            os.fsync(descriptor)
        except BaseException:
            body_failed = True
            raise
        finally:
            try:
                os.close(descriptor)
            except OSError:
                if not body_failed:
                    raise

    def _create_lineage_marker_unlocked(self) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.lineage_path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            written = os.write(descriptor, _LINEAGE_MARKER)
            if written != len(_LINEAGE_MARKER):
                raise OSError(errno.EIO, "short lineage marker write")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            self._fsync_parent(self.lineage_path)
        except OSError as exc:
            raise Stress90CrashFillRecoveryError(
                "crash-fill recovery lineage marker creation failed"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _validate_and_refsync_lineage_unlocked(self) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.lineage_path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, "lineage marker is not regular")
            payload = os.read(descriptor, len(_LINEAGE_MARKER) + 1)
            if payload != _LINEAGE_MARKER:
                raise Stress90CrashFillRecoveryError(
                    "crash-fill recovery lineage marker is invalid"
                )
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            self._fsync_parent(self.lineage_path)
        except OSError as exc:
            raise Stress90CrashFillRecoveryError(
                "crash-fill recovery lineage durability validation failed"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _decode_record(self, payload: bytes, label: str) -> Stress90CrashFillRecoveryRecord:
        try:
            raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Stress90CrashFillRecoveryError(
                f"invalid {label} crash-fill recovery JSON"
            ) from exc
        fields = {
            "kind",
            "schema_version",
            "sequence",
            "parent_checksum",
            "checkpoint",
            "operation_history",
            "checksum",
        }
        if not isinstance(raw, dict) or set(raw) != fields:
            raise Stress90CrashFillRecoveryError(f"{label} crash-fill recovery envelope is invalid")
        if raw["kind"] != _KIND or raw["schema_version"] != _SCHEMA:
            raise Stress90CrashFillRecoveryError("crash-fill recovery schema is unsupported")
        sequence = _positive_int(raw["sequence"], "recovery sequence")
        parent = raw["parent_checksum"]
        if sequence == 1:
            if parent is not None:
                raise Stress90CrashFillRecoveryError("initial recovery parent is invalid")
        else:
            parent = _sha(parent, "recovery parent checksum")
        checksum = _sha(raw["checksum"], "recovery checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != _digest(unsigned):
            raise Stress90CrashFillRecoveryError(f"{label} crash-fill recovery checksum mismatch")
        checkpoint = _decode_checkpoint(raw["checkpoint"])
        expected_status = "prepared" if sequence % 2 == 1 else "committed"
        if checkpoint.status != expected_status:
            raise Stress90CrashFillRecoveryError("recovery sequence/status parity is invalid")
        history_raw = raw["operation_history"]
        if not isinstance(history_raw, list):
            raise Stress90CrashFillRecoveryError("recovery operation history is invalid")
        history: list[Stress90CrashFillRecoveryOperation] = []
        for item in history_raw:
            if not isinstance(item, dict) or set(item) != {"operation_nonce", "request_digest"}:
                raise Stress90CrashFillRecoveryError("recovery operation history is invalid")
            history.append(
                Stress90CrashFillRecoveryOperation(
                    _sha(item["operation_nonce"], "recovery history nonce"),
                    _sha(item["request_digest"], "recovery history request digest"),
                )
            )
        if len({item.operation_nonce for item in history}) != len(history):
            raise Stress90CrashFillRecoveryError("recovery operation history reuses a nonce")
        if len(history) != (sequence + 1) // 2:
            raise Stress90CrashFillRecoveryError(
                "recovery operation history transition is truncated"
            )
        if not history or history[-1] != Stress90CrashFillRecoveryOperation(
            checkpoint.operation_nonce,
            checkpoint.transaction_id,
        ):
            raise Stress90CrashFillRecoveryError("recovery checkpoint/history mismatch")
        return Stress90CrashFillRecoveryRecord(
            checkpoint=checkpoint,
            operation_history=tuple(history),
            sequence=sequence,
            parent_checksum=parent,
            checksum=checksum,
        )

    def _load_unlocked(
        self,
        *,
        required: bool,
        surviving_visible_lock: bool,
    ) -> Stress90CrashFillRecoveryRecord | None:
        current_exists = self._exists(self.path)
        previous_exists = self._exists(self.previous_path)
        lineage_exists = self._exists(self.lineage_path)
        if not current_exists:
            evidence = []
            if previous_exists:
                evidence.append(".prev")
            if lineage_exists:
                evidence.append("lineage")
            if surviving_visible_lock:
                evidence.append("lock")
            if evidence:
                raise Stress90CrashFillRecoveryError(
                    "current crash-fill recovery is missing while evidence survives: "
                    + ", ".join(evidence)
                )
            if required:
                raise Stress90CrashFillRecoveryError(
                    "required current crash-fill recovery is missing"
                )
            return None
        if not lineage_exists:
            raise Stress90CrashFillRecoveryError(
                "current crash-fill recovery exists without lineage evidence"
            )
        self._validate_and_refsync_lineage_unlocked()
        current_bytes = self._read_bytes(self.path, "current")
        current = self._decode_record(current_bytes, "current")
        if current.sequence == 1:
            if previous_exists:
                raise Stress90CrashFillRecoveryError(
                    "unexpected recovery .prev evidence for initial sequence"
                )
            return current
        if not previous_exists:
            raise Stress90CrashFillRecoveryError("recovery .prev evidence is missing")
        previous_bytes = self._read_bytes(self.previous_path, "previous")
        if previous_bytes == current_bytes:
            return current
        previous = self._decode_record(previous_bytes, "previous")
        if (
            previous.sequence + 1 != current.sequence
            or current.parent_checksum != previous.checksum
        ):
            raise Stress90CrashFillRecoveryError("recovery parent chain mismatch")
        if current.sequence % 2 == 0:
            if (
                current.operation_history != previous.operation_history
                or current.checkpoint != replace(previous.checkpoint, status="committed")
            ):
                raise Stress90CrashFillRecoveryError(
                    "recovery committed history transition is invalid"
                )
        elif current.operation_history[:-1] != previous.operation_history:
            raise Stress90CrashFillRecoveryError("recovery prepared history transition is invalid")
        return current

    def _atomic_replace(self, path: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                "wb", dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            self._fsync_parent(path)
        except OSError as exc:
            raise Stress90CrashFillRecoveryError(
                "crash-fill recovery atomic replace failed"
            ) from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _save_unlocked(
        self,
        current: Stress90CrashFillRecoveryRecord | None,
        checkpoint: Stress90CrashFillRecoveryCheckpoint,
        history: tuple[Stress90CrashFillRecoveryOperation, ...],
    ) -> Stress90CrashFillRecoveryRecord:
        sequence = 1 if current is None else current.sequence + 1
        parent = None if current is None else current.checksum
        unsigned: dict[str, object] = {
            "kind": _KIND,
            "schema_version": _SCHEMA,
            "sequence": sequence,
            "parent_checksum": parent,
            "checkpoint": _checkpoint_payload(checkpoint),
            "operation_history": [_operation_payload(item) for item in history],
        }
        encoded = json.dumps(
            {**unsigned, "checksum": _digest(unsigned)},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if current is not None:
            self._atomic_replace(self.previous_path, self._read_bytes(self.path, "current"))
        self._atomic_replace(self.path, encoded)
        return self._decode_record(encoded, "current")

    def load_record(self) -> Stress90CrashFillRecoveryRecord | None:
        with self._exclusive_lock() as lock:
            return self._load_unlocked(
                required=False,
                surviving_visible_lock=lock.surviving_visible_lock,
            )

    def load_required_record(self) -> Stress90CrashFillRecoveryRecord:
        with self._exclusive_lock() as lock:
            record = self._load_unlocked(
                required=True,
                surviving_visible_lock=lock.surviving_visible_lock,
            )
            assert record is not None
            return record

    def load_previous_record(self) -> Stress90CrashFillRecoveryRecord:
        with self._exclusive_lock() as lock:
            del lock
            if not self._exists(self.previous_path):
                raise Stress90CrashFillRecoveryError("recovery .prev evidence is missing")
            return self._decode_record(self._read_bytes(self.previous_path, "previous"), "previous")

    def begin(
        self,
        checkpoint: Stress90CrashFillRecoveryCheckpoint,
    ) -> Stress90CrashFillRecoveryRecord:
        if checkpoint.status != "prepared":
            raise Stress90CrashFillRecoveryError("recovery begin requires prepared checkpoint")
        # Decode the exact payload before any mutation.
        checkpoint = _decode_checkpoint(_checkpoint_payload(checkpoint))
        requested = Stress90CrashFillRecoveryOperation(
            checkpoint.operation_nonce,
            checkpoint.transaction_id,
        )
        with self._exclusive_lock() as lock:
            current = self._load_unlocked(
                required=False,
                surviving_visible_lock=lock.surviving_visible_lock,
            )
            if current is not None:
                same_nonce = current.checkpoint.operation_nonce == checkpoint.operation_nonce
                if same_nonce and current.checkpoint.transaction_id == checkpoint.transaction_id:
                    return current
                if same_nonce:
                    raise Stress90CrashFillRecoveryError(
                        "recovery operation nonce was reused for a different request"
                    )
                if any(
                    item.operation_nonce == checkpoint.operation_nonce
                    for item in current.operation_history
                ):
                    raise Stress90CrashFillRecoveryError(
                        "recovery operation nonce was already consumed"
                    )
                if current.checkpoint.status == "prepared":
                    raise Stress90CrashFillRecoveryError(
                        "a crash-fill recovery transaction is already prepared"
                    )
                history = (*current.operation_history, requested)
            else:
                self._create_lineage_marker_unlocked()
                self._claim_initial_visible_lock_unlocked(lock)
                history = (requested,)
            return self._save_unlocked(current, checkpoint, history)

    def mark_committed(self, transaction_id: str) -> Stress90CrashFillRecoveryRecord:
        transaction = _sha(transaction_id, "recovery transaction identity")
        with self._exclusive_lock() as lock:
            current = self._load_unlocked(
                required=True,
                surviving_visible_lock=lock.surviving_visible_lock,
            )
            assert current is not None
            if current.checkpoint.transaction_id != transaction:
                raise Stress90CrashFillRecoveryError("crash-fill recovery changed before commit")
            if current.checkpoint.status == "committed":
                return current
            return self._save_unlocked(
                current,
                replace(current.checkpoint, status="committed"),
                current.operation_history,
            )


def _state_is_source(
    record: RuntimeStateRecord,
    checkpoint: Stress90CrashFillRecoveryCheckpoint,
) -> bool:
    return (
        record.sequence == checkpoint.generic_source_sequence
        and record.checksum == checkpoint.generic_source_checksum
    )


def _state_is_target(
    record: RuntimeStateRecord,
    checkpoint: Stress90CrashFillRecoveryCheckpoint,
) -> bool:
    return (
        record.sequence == checkpoint.generic_target_sequence
        and record.checksum == checkpoint.generic_target_checksum
        and record.state == checkpoint.generic_target
    )


def apply_stress90_crash_fill_recovery(
    recovery_store: Stress90CrashFillRecoveryStore,
    state_store: StateStore,
) -> Stress90CrashFillRecoveryRecord:
    """Idempotently roll one prepared checkpoint forward; never roll state backward."""

    record = recovery_store.load_required_record()
    checkpoint = record.checkpoint
    state = state_store.load_required_record()
    if checkpoint.status == "committed":
        if not _state_is_target(state, checkpoint):
            raise Stress90CrashFillRecoveryError(
                "committed recovery has an unrelated generic state revision"
            )
        return record
    if _state_is_source(state, checkpoint):
        state_store.save(
            checkpoint.generic_target,
            expected_sequence=checkpoint.generic_source_sequence,
            expected_checksum=checkpoint.generic_source_checksum,
        )
    elif not _state_is_target(state, checkpoint):
        raise Stress90CrashFillRecoveryError(
            "prepared recovery has an unrelated generic state revision"
        )
    final = state_store.load_required_record()
    if not _state_is_target(final, checkpoint):
        raise Stress90CrashFillRecoveryError("recovery generic target is incomplete")
    return recovery_store.mark_committed(checkpoint.transaction_id)


def require_committed_stress90_crash_fill_recovery(
    recovery_store: Stress90CrashFillRecoveryStore,
    state_record: RuntimeStateRecord,
) -> Stress90CrashFillRecoveryRecord:
    """Consume one exact committed proof before a lifecycle command uses generic state."""

    marker = state_record.state.strategy_states.get(STRESS90_CRASH_FILL_RECOVERY_STATE_KEY)
    if marker is None:
        raise Stress90CrashFillRecoveryError("generic state has no crash-fill recovery proof")
    if not isinstance(marker, dict) or set(marker) != {"transaction_id"}:
        raise Stress90CrashFillRecoveryError("generic crash-fill recovery marker is invalid")
    record = recovery_store.load_required_record()
    if (
        record.checkpoint.status != "committed"
        or marker["transaction_id"] != record.checkpoint.transaction_id
        or not _state_is_target(state_record, record.checkpoint)
    ):
        raise Stress90CrashFillRecoveryError(
            "committed recovery does not prove the exact generic state"
        )
    return record
