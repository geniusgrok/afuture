"""Operator-managed Stress-90 account-continuity evidence.

This module records an explicit single-operator trust assertion.  It is deliberately
separate from broker/exchange settlement evidence and never upgrades broker verification
flags.  The receipt is a local, checksummed audit artifact that is additionally anchored
by the machine account-runtime registry operation nonce.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

_KIND = "afuture.stress90-operator-continuity"
_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_FILE_BYTES = 1_000_000
_MAX_RUNTIME_PATH = 4_096
_LINEAGE_MARKER = b'{"kind":"afuture.stress90-operator-continuity-lineage","schema_version":1}\n'
STRESS90_OPERATOR_CONTINUITY_CONFIRMATION = "I_CONFIRM_EXCLUSIVE_ACCOUNT_AND_NO_EXTERNAL_ACTIVITY"
_OPERATOR_CONFIRMATION_TYPE = "exclusive_account_no_external_activity"


class Stress90OperatorContinuityError(RuntimeError):
    """Operator continuity evidence is absent, malformed, stale, or inconsistent."""


@dataclass(frozen=True)
class Stress90OperatorContinuityEvidence:
    operation_id: str
    operator_reason: str
    confirmation_type: str
    source_ctp_trading_day: str
    target_ctp_trading_day: str
    natural_day_gap: int
    source_generic_state_sequence: int
    source_generic_state_checksum: str
    source_policy_state_sequence: int
    source_policy_state_checksum: str
    source_trading_day_evidence_sequence: int
    source_trading_day_evidence_checksum: str
    account_identity_digest: str
    account_epoch: str
    canonical_runtime: str
    canonical_runtime_digest: str
    source_account_registry_receipt_digest: str
    fresh_account_snapshot_digest: str
    position_reconciliation_digest: str
    session_ownership_digest: str
    ctp_order_journal_digest: str
    active_order_count: int
    deposit: float
    withdrawal: float
    source_last_account_equity: float
    source_day_start_equity: float
    source_hwm_equity: float
    source_settlement_id: str
    target_previous_settlement_equity: float
    target_settlement_id: str
    ohlc_latest_completed_day: str
    activity_latest_completed_day: str
    oi_latest_completed_day: str
    policy_latest_completed_day: str
    no_manual_trade: bool
    no_external_order: bool
    no_deposit: bool
    no_withdrawal: bool
    evidence_authority: str
    authoritative_broker_or_exchange_evidence: bool


@dataclass(frozen=True)
class Stress90OperatorAccountDayContinuityEvidence:
    completed_account_day: str
    current_ctp_trading_day: str
    natural_day_gap: int
    ohlc_content_digest: str
    oi_store_checksum: str
    completed_oi_evidence_digest: str
    observed_transition_digest: str
    continuity_digest: str


def load_stress90_operator_account_day_continuity_evidence(
    ohlc_store,
    oi_store,
    *,
    completed_account_day: str,
    current_ctp_trading_day: str,
) -> Stress90OperatorAccountDayContinuityEvidence:
    """Prove one observed market-session transition without guessing a trading calendar."""

    from .directional_ohlc_refresh import load_stress90_completed_ohlc
    from .directional_stress90_policy import STRESS90_POLICY
    from .directional_stress90_runtime import stress90_target_transitions

    source_dt = _day(completed_account_day, "completed account trading day")
    target_dt = _day(current_ctp_trading_day, "current CTP trading day")
    if target_dt <= source_dt:
        raise Stress90OperatorContinuityError(
            "operator continuity target CTP day must be strictly later than source day"
        )
    source = source_dt.strftime("%Y%m%d")
    target = target_dt.strftime("%Y%m%d")
    natural_gap = (target_dt.date() - source_dt.date()).days
    try:
        entry = load_stress90_completed_ohlc(
            ohlc_store,
            products=STRESS90_POLICY.products,
            current_ctp_trading_day=target,
            authoritative_ctp_trading_day=target,
            required_completed_day=source,
        )
        transitions = stress90_target_transitions(
            last_completed_target_day=source,
            current_ctp_trading_day=target,
            completed_close_index=entry.close.index,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise Stress90OperatorContinuityError(
            "operator continuity OHLC session chain is not contiguous"
        ) from exc
    if transitions != ((source, target),):
        raise Stress90OperatorContinuityError(
            "operator continuity cannot skip intermediate completed OHLC session data"
        )

    try:
        oi_record = oi_store.load_required_record()
        completed_rows = tuple(
            item for item in oi_record.state.completed if source <= item.trading_day < target
        )
        observed = tuple(
            item
            for item in oi_record.state.observed_transitions
            if item.source_trading_day == source and item.target_trading_day == target
        )
    except Exception as exc:
        raise Stress90OperatorContinuityError(
            "operator continuity OI session evidence is unavailable"
        ) from exc
    if len(completed_rows) != 1 or completed_rows[0].trading_day != source:
        raise Stress90OperatorContinuityError(
            "operator continuity cannot skip intermediate completed OI session data"
        )
    completed = completed_rows[0]
    if (
        completed.complete is not True
        or set(completed.flows) != set(STRESS90_POLICY.oi_products)
        or any(value not in (-1, 0, 1) for value in completed.flows.values())
    ):
        raise Stress90OperatorContinuityError(
            "operator continuity completed OI evidence is incomplete"
        )
    if len(observed) != 1:
        raise Stress90OperatorContinuityError(
            "operator continuity requires one observed CTP source/target transition"
        )
    transition = observed[0]
    if transition.completed_oi_evidence_digest != completed.evidence_digest:
        raise Stress90OperatorContinuityError(
            "operator continuity observed transition/OI digest mismatch"
        )
    observed_transition_digest = _digest(
        {
            "source_trading_day": transition.source_trading_day,
            "target_trading_day": transition.target_trading_day,
            "completed_oi_evidence_digest": transition.completed_oi_evidence_digest,
        }
    )
    identity = {
        "kind": "afuture.stress90-operator-market-session-continuity",
        "schema_version": 1,
        "completed_account_day": source,
        "current_ctp_trading_day": target,
        "natural_day_gap": natural_gap,
        "ohlc_content_digest": entry.content_digest,
        "oi_store_checksum": oi_record.checksum,
        "completed_oi_evidence_digest": completed.evidence_digest,
        "observed_transition_digest": observed_transition_digest,
    }
    return Stress90OperatorAccountDayContinuityEvidence(
        completed_account_day=source,
        current_ctp_trading_day=target,
        natural_day_gap=natural_gap,
        ohlc_content_digest=entry.content_digest,
        oi_store_checksum=oi_record.checksum,
        completed_oi_evidence_digest=completed.evidence_digest,
        observed_transition_digest=observed_transition_digest,
        continuity_digest=_digest(identity),
    )


@dataclass(frozen=True)
class Stress90OperatorRollForwardPlan:
    targets: object
    evidence: Stress90OperatorContinuityEvidence
    request_digest: str


def build_stress90_operator_roll_forward_plan(
    *,
    operation_id: str,
    operator_reason: str,
    generic_source,
    policy_source,
    source_trading_day_evidence,
    source_registry_evidence,
    runtime_dir: str | Path,
    account_identity_digest: str,
    account_snapshot,
    active_order_count: int,
    positions_reconciled: bool,
    position_reconciliation_digest: str,
    session_ownership_digest: str,
    ctp_order_journal_digest: str,
    activity_latest_completed_day: str,
    market_continuity: Stress90OperatorAccountDayContinuityEvidence,
) -> Stress90OperatorRollForwardPlan:
    """Build one operator-trust rollover using the existing settlement target math."""

    from .models import AccountSnapshot, RuntimeMode
    from .stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        build_stress90_settlement_roll_forward_targets,
        stress90_lifecycle_account_evidence_digest,
    )

    operation = _sha(operation_id, "operator continuity operation ID")
    if not isinstance(operator_reason, str) or not operator_reason.strip():
        raise Stress90OperatorContinuityError("operator continuity reason is required")
    reason = operator_reason.strip()
    if reason != operator_reason or len(reason) > 1_000:
        raise Stress90OperatorContinuityError("operator continuity reason is invalid")
    if not isinstance(account_snapshot, AccountSnapshot):
        raise Stress90OperatorContinuityError("fresh account snapshot is invalid")
    if type(active_order_count) is not int or active_order_count != 0:
        raise Stress90OperatorContinuityError("operator continuity requires zero active orders")
    if positions_reconciled is not True:
        raise Stress90OperatorContinuityError(
            "operator continuity requires exact Broker/local position reconciliation"
        )
    for digest_value, name in (
        (position_reconciliation_digest, "position reconciliation digest"),
        (session_ownership_digest, "session ownership digest"),
        (ctp_order_journal_digest, "CTP order journal digest"),
    ):
        _sha(digest_value, name)

    generic_sequence = _positive_sequence(
        getattr(generic_source, "sequence", None), "source generic state sequence"
    )
    generic_checksum = _sha(
        getattr(generic_source, "checksum", None), "source generic state checksum"
    )
    generic_state = getattr(generic_source, "state", None)
    if generic_state is None:
        raise Stress90OperatorContinuityError("source generic state is missing")
    if (
        getattr(generic_state, "runtime_mode", None) != RuntimeMode.HALTED.value
        or getattr(generic_state, "kill_switch", None) is not True
        or getattr(generic_state, "reconciled", None) is not True
    ):
        raise Stress90OperatorContinuityError(
            "operator continuity requires a HALTED, kill-switched, reconciled source"
        )

    policy_sequence = _positive_sequence(
        getattr(policy_source, "sequence", None), "source policy state sequence"
    )
    policy_checksum = _sha(getattr(policy_source, "checksum", None), "source policy state checksum")
    policy_state = getattr(policy_source, "state", None)
    if policy_state is None:
        raise Stress90OperatorContinuityError("source policy state is missing")

    source_day = str(getattr(generic_state, "trading_day", "") or "")
    if (
        not source_day
        or getattr(generic_state, "last_account_trading_day", None) != source_day
        or getattr(policy_state, "last_completed_target_day", None) != source_day
        or market_continuity.completed_account_day != source_day
        or activity_latest_completed_day != source_day
    ):
        raise Stress90OperatorContinuityError(
            "generic/policy/activity/market source trading day alignment is invalid"
        )
    _day(source_day, "operator continuity source day")
    if getattr(
        policy_state, "live_account_identity_digest", None
    ) != account_identity_digest or not getattr(policy_state, "live_account_epoch", None):
        raise Stress90OperatorContinuityError(
            "operator continuity policy account identity/epoch mismatch"
        )
    account_epoch = _sha(
        policy_state.live_account_epoch, "operator continuity policy account epoch"
    )
    account_identity = _sha(account_identity_digest, "operator continuity account identity")

    canonical_runtime, runtime_digest = _canonical_runtime(os.fspath(runtime_dir))
    binding = getattr(source_registry_evidence, "binding", None)
    source_registry_receipt_digest = _sha(
        getattr(source_registry_evidence, "binding_receipt_digest", None),
        "source account registry receipt digest",
    )
    if (
        binding is None
        or getattr(binding, "account_identity_digest", None) != account_identity
        or getattr(binding, "account_epoch", None) != account_epoch
        or getattr(binding, "canonical_runtime", None) != canonical_runtime
        or getattr(binding, "runtime_identity_digest", None) != runtime_digest
    ):
        raise Stress90OperatorContinuityError(
            "operator continuity source account registry binding mismatch"
        )
    if (
        getattr(source_trading_day_evidence, "phase", None) != "bound"
        or getattr(source_trading_day_evidence, "trading_day", None) != source_day
        or getattr(source_trading_day_evidence, "account_identity_digest", None) != account_identity
        or getattr(source_trading_day_evidence, "account_epoch", None) != account_epoch
        or getattr(source_trading_day_evidence, "canonical_runtime", None) != canonical_runtime
        or getattr(source_trading_day_evidence, "runtime_identity_digest", None) != runtime_digest
        or getattr(source_trading_day_evidence, "account_binding_receipt_digest", None)
        != source_registry_receipt_digest
        or getattr(source_trading_day_evidence, "registry_sequence", None)
        != getattr(source_registry_evidence, "registry_sequence", None)
        or getattr(source_trading_day_evidence, "registry_checksum", None)
        != getattr(source_registry_evidence, "registry_checksum", None)
    ):
        raise Stress90OperatorContinuityError(
            "operator continuity source TradingDayEvidence/registry receipt mismatch"
        )
    tde_sequence = _positive_sequence(
        getattr(source_trading_day_evidence, "sequence", None),
        "source TradingDayEvidence sequence",
    )
    tde_checksum = _sha(
        getattr(source_trading_day_evidence, "checksum", None),
        "source TradingDayEvidence checksum",
    )

    target_day = market_continuity.current_ctp_trading_day
    if account_snapshot.trading_day != target_day:
        raise Stress90OperatorContinuityError(
            "fresh account snapshot does not match target CTP trading day"
        )
    if float(account_snapshot.deposit) != 0.0 or float(account_snapshot.withdrawal) != 0.0:
        raise Stress90OperatorContinuityError(
            "operator continuity requires Deposit=0 and Withdraw=0; use stress90-account-rebase"
        )
    try:
        fresh_account_digest = stress90_lifecycle_account_evidence_digest(
            account_snapshot,
            account_identity_digest=account_identity,
            scope="settlement_snapshot",
        )
        targets = build_stress90_settlement_roll_forward_targets(
            generic_state,
            policy_state,
            account_snapshot,
        )
    except (Stress90LifecycleTransactionError, TypeError, ValueError) as exc:
        raise Stress90OperatorContinuityError(
            "operator continuity settlement/account path is not safely roll-forwardable"
        ) from exc
    if (
        targets.completed_account_day != source_day
        or targets.current_ctp_trading_day != target_day
        or targets.generic_target.runtime_mode != RuntimeMode.HALTED.value
        or targets.generic_target.kill_switch is not True
        or targets.generic_target.metadata_verified is not False
    ):
        raise Stress90OperatorContinuityError(
            "operator continuity target does not preserve HALTED fail-closed state"
        )

    settlement_raw = getattr(account_snapshot, "previous_settlement_equity", None)
    settlement_id = getattr(account_snapshot, "settlement_id", None)
    if settlement_raw is None or settlement_id is None:
        raise Stress90OperatorContinuityError(
            "operator continuity target settlement identity is missing"
        )
    source_settlement_id = getattr(generic_state, "last_account_settlement_id", None)
    if type(source_settlement_id) is not int or source_settlement_id < 0:
        raise Stress90OperatorContinuityError(
            "operator continuity source settlement identity is missing"
        )
    natural_gap = market_continuity.natural_day_gap
    expected_gap = (
        _day(target_day, "operator continuity target day").date()
        - _day(source_day, "operator continuity source day").date()
    ).days
    if natural_gap != expected_gap:
        raise Stress90OperatorContinuityError(
            "operator continuity natural-day gap evidence is inconsistent"
        )

    evidence = Stress90OperatorContinuityEvidence(
        operation_id=operation,
        operator_reason=reason,
        confirmation_type=_OPERATOR_CONFIRMATION_TYPE,
        source_ctp_trading_day=source_day,
        target_ctp_trading_day=target_day,
        natural_day_gap=natural_gap,
        source_generic_state_sequence=generic_sequence,
        source_generic_state_checksum=generic_checksum,
        source_policy_state_sequence=policy_sequence,
        source_policy_state_checksum=policy_checksum,
        source_trading_day_evidence_sequence=tde_sequence,
        source_trading_day_evidence_checksum=tde_checksum,
        account_identity_digest=account_identity,
        account_epoch=account_epoch,
        canonical_runtime=canonical_runtime,
        canonical_runtime_digest=runtime_digest,
        source_account_registry_receipt_digest=source_registry_receipt_digest,
        fresh_account_snapshot_digest=fresh_account_digest,
        position_reconciliation_digest=position_reconciliation_digest,
        session_ownership_digest=session_ownership_digest,
        ctp_order_journal_digest=ctp_order_journal_digest,
        active_order_count=0,
        deposit=float(account_snapshot.deposit),
        withdrawal=float(account_snapshot.withdrawal),
        source_last_account_equity=float(generic_state.last_account_equity),
        source_day_start_equity=float(generic_state.day_start_equity),
        source_hwm_equity=float(generic_state.equity_high_watermark),
        source_settlement_id=str(source_settlement_id),
        target_previous_settlement_equity=float(settlement_raw),
        target_settlement_id=str(settlement_id),
        ohlc_latest_completed_day=source_day,
        activity_latest_completed_day=source_day,
        oi_latest_completed_day=source_day,
        policy_latest_completed_day=source_day,
        no_manual_trade=True,
        no_external_order=True,
        no_deposit=True,
        no_withdrawal=True,
        evidence_authority="operator_trust",
        authoritative_broker_or_exchange_evidence=False,
    )
    request_digest = stress90_operator_continuity_request_digest(evidence)
    return Stress90OperatorRollForwardPlan(
        targets=targets,
        evidence=evidence,
        request_digest=request_digest,
    )


@dataclass(frozen=True)
class Stress90OperatorContinuityRecord:
    sequence: int
    parent_checksum: str | None
    evidence: Stress90OperatorContinuityEvidence
    request_digest: str
    target_registry_receipt_digest: str
    target_trading_day_evidence_sequence: int
    target_trading_day_evidence_checksum: str
    checksum: str
    schema_version: int = _SCHEMA_VERSION


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Stress90OperatorContinuityError(
            "operator continuity evidence is not finite canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise Stress90OperatorContinuityError(f"{name} must be lowercase SHA-256")
    return value


def _positive_sequence(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise Stress90OperatorContinuityError(f"{name} must be a positive integer")
    return value


def _day(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        raise Stress90OperatorContinuityError(f"{name} must be YYYYMMDD")
    try:
        return datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise Stress90OperatorContinuityError(f"{name} must be a valid YYYYMMDD") from exc


def _finite(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Stress90OperatorContinuityError(f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise Stress90OperatorContinuityError(f"{name} must be finite")
    return result


def _canonical_runtime(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or not value or len(value) > _MAX_RUNTIME_PATH:
        raise Stress90OperatorContinuityError("canonical runtime binding is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise Stress90OperatorContinuityError("canonical runtime binding must be absolute")
    canonical = os.path.realpath(os.path.normpath(value))
    if canonical == os.path.abspath(os.sep) or len(canonical) > _MAX_RUNTIME_PATH:
        raise Stress90OperatorContinuityError("canonical runtime binding is invalid")
    return canonical, sha256(f"runtime:{canonical}".encode()).hexdigest()


def _validate_evidence(
    evidence: Stress90OperatorContinuityEvidence,
) -> Stress90OperatorContinuityEvidence:
    _sha(evidence.operation_id, "operator continuity operation ID")
    reason = evidence.operator_reason
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or reason != reason.strip()
        or len(reason) > 1_000
    ):
        raise Stress90OperatorContinuityError("operator reason is invalid")
    if evidence.confirmation_type != _OPERATOR_CONFIRMATION_TYPE:
        raise Stress90OperatorContinuityError("operator confirmation type is invalid")
    source = _day(evidence.source_ctp_trading_day, "source CTP trading day")
    target = _day(evidence.target_ctp_trading_day, "target CTP trading day")
    if target <= source:
        raise Stress90OperatorContinuityError(
            "target CTP trading day must be strictly later than source day"
        )
    natural_gap = (target.date() - source.date()).days
    if type(evidence.natural_day_gap) is not int or evidence.natural_day_gap != natural_gap:
        raise Stress90OperatorContinuityError("natural day gap does not match source/target days")

    for sequence_value, name in (
        (evidence.source_generic_state_sequence, "source generic state sequence"),
        (evidence.source_policy_state_sequence, "source policy state sequence"),
        (
            evidence.source_trading_day_evidence_sequence,
            "source TradingDayEvidence sequence",
        ),
    ):
        _positive_sequence(sequence_value, name)
    for digest_value, name in (
        (evidence.source_generic_state_checksum, "source generic state checksum"),
        (evidence.source_policy_state_checksum, "source policy state checksum"),
        (
            evidence.source_trading_day_evidence_checksum,
            "source TradingDayEvidence checksum",
        ),
        (evidence.account_identity_digest, "account identity digest"),
        (evidence.account_epoch, "account epoch"),
        (
            evidence.source_account_registry_receipt_digest,
            "source account registry receipt digest",
        ),
        (evidence.fresh_account_snapshot_digest, "fresh account snapshot digest"),
        (evidence.position_reconciliation_digest, "position reconciliation digest"),
        (evidence.session_ownership_digest, "session ownership digest"),
        (evidence.ctp_order_journal_digest, "CTP order journal digest"),
    ):
        _sha(digest_value, name)

    canonical, runtime_digest = _canonical_runtime(evidence.canonical_runtime)
    if evidence.canonical_runtime != canonical:
        raise Stress90OperatorContinuityError("canonical runtime binding is not canonical")
    if _sha(evidence.canonical_runtime_digest, "canonical runtime digest") != runtime_digest:
        raise Stress90OperatorContinuityError("canonical runtime digest binding mismatch")

    if type(evidence.active_order_count) is not int or evidence.active_order_count != 0:
        raise Stress90OperatorContinuityError("operator continuity requires zero active orders")
    deposit = _finite(evidence.deposit, "Deposit")
    withdrawal = _finite(evidence.withdrawal, "Withdraw")
    if deposit != 0.0 or withdrawal != 0.0:
        raise Stress90OperatorContinuityError(
            "operator continuity requires current Deposit/Withdraw to both be zero"
        )
    for equity_value, name in (
        (evidence.source_last_account_equity, "source last-account equity"),
        (evidence.source_day_start_equity, "source day-start equity"),
        (evidence.source_hwm_equity, "source HWM equity"),
        (
            evidence.target_previous_settlement_equity,
            "target previous-settlement equity",
        ),
    ):
        _finite(equity_value, name, positive=True)
    if (
        not isinstance(evidence.source_settlement_id, str)
        or not evidence.source_settlement_id.strip()
        or len(evidence.source_settlement_id) > 128
        or not isinstance(evidence.target_settlement_id, str)
        or not evidence.target_settlement_id.strip()
        or len(evidence.target_settlement_id) > 128
    ):
        raise Stress90OperatorContinuityError("settlement identity is invalid")

    source_day = evidence.source_ctp_trading_day
    if any(
        value != source_day
        for value in (
            evidence.ohlc_latest_completed_day,
            evidence.activity_latest_completed_day,
            evidence.oi_latest_completed_day,
            evidence.policy_latest_completed_day,
        )
    ):
        raise Stress90OperatorContinuityError(
            "policy/OHLC/activity/OI latest completed day must equal source day"
        )
    if not all(
        value is True
        for value in (
            evidence.no_manual_trade,
            evidence.no_external_order,
            evidence.no_deposit,
            evidence.no_withdrawal,
        )
    ):
        raise Stress90OperatorContinuityError(
            "all no-external-activity operator assertions are required"
        )
    if evidence.evidence_authority != "operator_trust":
        raise Stress90OperatorContinuityError(
            "operator continuity evidence authority must be operator_trust"
        )
    if evidence.authoritative_broker_or_exchange_evidence is not False:
        raise Stress90OperatorContinuityError(
            "operator continuity must never be marked authoritative broker/exchange evidence"
        )
    return evidence


def stress90_operator_continuity_request_digest(
    evidence: Stress90OperatorContinuityEvidence,
) -> str:
    """Return the immutable semantic digest anchored by the registry nonce ledger."""

    _validate_evidence(evidence)
    return _digest(
        {
            "kind": "afuture.stress90-operator-continuity-request",
            "schema_version": 1,
            "evidence": asdict(evidence),
        }
    )


def _record_payload(
    *,
    sequence: int,
    parent_checksum: str | None,
    evidence: Stress90OperatorContinuityEvidence,
    request_digest: str,
    target_registry_receipt_digest: str,
    target_trading_day_evidence_sequence: int,
    target_trading_day_evidence_checksum: str,
) -> dict[str, object]:
    return {
        "kind": _KIND,
        "schema_version": _SCHEMA_VERSION,
        "sequence": sequence,
        "parent_checksum": parent_checksum,
        "evidence": asdict(evidence),
        "request_digest": request_digest,
        "target_registry_receipt_digest": target_registry_receipt_digest,
        "target_trading_day_evidence_sequence": target_trading_day_evidence_sequence,
        "target_trading_day_evidence_checksum": target_trading_day_evidence_checksum,
    }


def _new_record(
    *,
    sequence: int,
    parent_checksum: str | None,
    evidence: Stress90OperatorContinuityEvidence,
    request_digest: str,
    target_registry_receipt_digest: str,
    target_trading_day_evidence_sequence: int,
    target_trading_day_evidence_checksum: str,
) -> Stress90OperatorContinuityRecord:
    payload = _record_payload(
        sequence=sequence,
        parent_checksum=parent_checksum,
        evidence=evidence,
        request_digest=request_digest,
        target_registry_receipt_digest=target_registry_receipt_digest,
        target_trading_day_evidence_sequence=target_trading_day_evidence_sequence,
        target_trading_day_evidence_checksum=target_trading_day_evidence_checksum,
    )
    return Stress90OperatorContinuityRecord(
        sequence=sequence,
        parent_checksum=parent_checksum,
        evidence=evidence,
        request_digest=request_digest,
        target_registry_receipt_digest=target_registry_receipt_digest,
        target_trading_day_evidence_sequence=target_trading_day_evidence_sequence,
        target_trading_day_evidence_checksum=target_trading_day_evidence_checksum,
        checksum=_digest(payload),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stress90OperatorContinuityError(f"duplicate operator continuity key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise Stress90OperatorContinuityError(
        f"operator continuity JSON contains non-finite constant: {value}"
    )


def _decode_record(data: bytes) -> Stress90OperatorContinuityRecord:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise Stress90OperatorContinuityError(
            "operator continuity artifact is not valid UTF-8"
        ) from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except Stress90OperatorContinuityError:
        raise
    except json.JSONDecodeError as exc:
        raise Stress90OperatorContinuityError(
            "operator continuity artifact is invalid JSON"
        ) from exc
    if not isinstance(raw, dict):
        raise Stress90OperatorContinuityError("operator continuity JSON root must be an object")
    expected = {
        "kind",
        "schema_version",
        "sequence",
        "parent_checksum",
        "evidence",
        "request_digest",
        "target_registry_receipt_digest",
        "target_trading_day_evidence_sequence",
        "target_trading_day_evidence_checksum",
        "checksum",
    }
    if set(raw) != expected or raw.get("kind") != _KIND or raw.get("schema_version") != 1:
        raise Stress90OperatorContinuityError("operator continuity artifact schema is invalid")
    sequence = _positive_sequence(raw["sequence"], "operator continuity sequence")
    parent_raw = raw["parent_checksum"]
    parent_checksum: str | None
    if parent_raw is None:
        parent_checksum = None
    else:
        parent_checksum = _sha(parent_raw, "operator continuity parent checksum")
    evidence_raw = raw["evidence"]
    if not isinstance(evidence_raw, dict):
        raise Stress90OperatorContinuityError("operator continuity evidence must be an object")
    expected_evidence = set(Stress90OperatorContinuityEvidence.__dataclass_fields__)
    if set(evidence_raw) != expected_evidence:
        raise Stress90OperatorContinuityError("operator continuity evidence schema is invalid")
    try:
        evidence = Stress90OperatorContinuityEvidence(**evidence_raw)
    except TypeError as exc:
        raise Stress90OperatorContinuityError("operator continuity evidence is invalid") from exc
    _validate_evidence(evidence)
    request_digest = _sha(raw["request_digest"], "operator continuity request digest")
    if request_digest != stress90_operator_continuity_request_digest(evidence):
        raise Stress90OperatorContinuityError("operator continuity request digest mismatch")
    target_registry = _sha(
        raw["target_registry_receipt_digest"],
        "target account registry receipt digest",
    )
    target_tde_sequence = _positive_sequence(
        raw["target_trading_day_evidence_sequence"],
        "target TradingDayEvidence sequence",
    )
    target_tde_checksum = _sha(
        raw["target_trading_day_evidence_checksum"],
        "target TradingDayEvidence checksum",
    )
    payload = _record_payload(
        sequence=sequence,
        parent_checksum=parent_checksum,
        evidence=evidence,
        request_digest=request_digest,
        target_registry_receipt_digest=target_registry,
        target_trading_day_evidence_sequence=target_tde_sequence,
        target_trading_day_evidence_checksum=target_tde_checksum,
    )
    checksum = _sha(raw["checksum"], "operator continuity checksum")
    if checksum != _digest(payload):
        raise Stress90OperatorContinuityError("operator continuity checksum mismatch")
    return Stress90OperatorContinuityRecord(
        sequence,
        parent_checksum,
        evidence,
        request_digest,
        target_registry,
        target_tde_sequence,
        target_tde_checksum,
        checksum,
    )


class Stress90OperatorContinuityStore:
    """Fail-closed current/.prev operator continuity receipt chain."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.previous_path = self.path.with_name(f"{self.path.name}.prev")
        self.lineage_path = self.path.with_name(f"{self.path.name}.lineage")

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise Stress90OperatorContinuityError(
                    "operator continuity artifact must be a regular file, not a symlink/directory"
                )
            if before.st_size > _MAX_FILE_BYTES:
                raise Stress90OperatorContinuityError("operator continuity artifact is too large")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65_536, _MAX_FILE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_FILE_BYTES:
                    raise Stress90OperatorContinuityError(
                        "operator continuity artifact is too large"
                    )
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
            ):
                raise Stress90OperatorContinuityError(
                    "operator continuity artifact changed while being read"
                )
            return b"".join(chunks)
        except Stress90OperatorContinuityError:
            raise
        except OSError as exc:
            raise Stress90OperatorContinuityError(
                "operator continuity artifact cannot be read as a regular file"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _require_lineage_marker(self) -> None:
        try:
            marker = self._read_bytes(self.lineage_path)
        except Stress90OperatorContinuityError as exc:
            raise Stress90OperatorContinuityError(
                "operator continuity lineage marker is missing or invalid"
            ) from exc
        if marker != _LINEAGE_MARKER:
            raise Stress90OperatorContinuityError(
                "operator continuity lineage marker is missing or invalid"
            )

    def _create_lineage_marker(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        try:
            flags = (
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(self.lineage_path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(_LINEAGE_MARKER)
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_parent()
        except FileExistsError:
            self._require_lineage_marker()
        except OSError as exc:
            raise Stress90OperatorContinuityError(
                "operator continuity lineage marker creation failed"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _fsync_parent(self) -> None:
        descriptor = os.open(
            self.path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_replace(self, path: Path, data: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with NamedTemporaryFile(
                mode="wb",
                dir=self.path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_parent()
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass
            raise Stress90OperatorContinuityError(
                "operator continuity artifact durable replace failed"
            ) from exc

    def load_record(self) -> Stress90OperatorContinuityRecord | None:
        current_exists = self._exists(self.path)
        previous_exists = self._exists(self.previous_path)
        lineage_exists = self._exists(self.lineage_path)
        if not current_exists:
            if previous_exists or lineage_exists:
                raise Stress90OperatorContinuityError(
                    "operator continuity current is missing while prior lineage exists"
                )
            return None
        self._require_lineage_marker()
        current = _decode_record(self._read_bytes(self.path))
        if current.sequence == 1:
            if current.parent_checksum is not None:
                raise Stress90OperatorContinuityError(
                    "first operator continuity receipt has a parent checksum"
                )
            if previous_exists:
                raise Stress90OperatorContinuityError(
                    "first operator continuity receipt unexpectedly has .prev"
                )
            return current
        if current.parent_checksum is None or not previous_exists:
            raise Stress90OperatorContinuityError("operator continuity .prev chain is missing")
        previous = _decode_record(self._read_bytes(self.previous_path))
        if previous == current:
            return current
        if (
            previous.sequence != current.sequence - 1
            or previous.checksum != current.parent_checksum
        ):
            raise Stress90OperatorContinuityError(
                "operator continuity current/.prev chain is inconsistent"
            )
        return current

    def load_required_record(self) -> Stress90OperatorContinuityRecord:
        record = self.load_record()
        if record is None:
            raise Stress90OperatorContinuityError("operator continuity current receipt is missing")
        return record

    def load_previous_record(self) -> Stress90OperatorContinuityRecord | None:
        current = self.load_record()
        if current is None or not self._exists(self.previous_path):
            return None
        previous = _decode_record(self._read_bytes(self.previous_path))
        if previous == current:
            return current
        if (
            previous.sequence != current.sequence - 1
            or previous.checksum != current.parent_checksum
        ):
            raise Stress90OperatorContinuityError(
                "operator continuity current/.prev chain is inconsistent"
            )
        return previous

    def save(
        self,
        evidence: Stress90OperatorContinuityEvidence,
        *,
        target_registry_receipt_digest: str,
        target_trading_day_evidence_sequence: int,
        target_trading_day_evidence_checksum: str,
    ) -> Stress90OperatorContinuityRecord:
        request_digest = stress90_operator_continuity_request_digest(evidence)
        registry_digest = _sha(
            target_registry_receipt_digest,
            "target account registry receipt digest",
        )
        tde_sequence = _positive_sequence(
            target_trading_day_evidence_sequence,
            "target TradingDayEvidence sequence",
        )
        tde_checksum = _sha(
            target_trading_day_evidence_checksum,
            "target TradingDayEvidence checksum",
        )
        current = self.load_record()
        if current is not None and current.evidence.operation_id == evidence.operation_id:
            candidate = _new_record(
                sequence=current.sequence,
                parent_checksum=current.parent_checksum,
                evidence=evidence,
                request_digest=request_digest,
                target_registry_receipt_digest=registry_digest,
                target_trading_day_evidence_sequence=tde_sequence,
                target_trading_day_evidence_checksum=tde_checksum,
            )
            if candidate == current:
                return current
            raise Stress90OperatorContinuityError(
                "operator continuity operation ID was reused for a different request"
            )
        if current is not None:
            if (
                evidence.source_ctp_trading_day != current.evidence.target_ctp_trading_day
                or evidence.source_trading_day_evidence_sequence
                != current.target_trading_day_evidence_sequence
                or evidence.source_trading_day_evidence_checksum
                != current.target_trading_day_evidence_checksum
                or evidence.source_account_registry_receipt_digest
                != current.target_registry_receipt_digest
            ):
                raise Stress90OperatorContinuityError(
                    "operator continuity source does not match the current receipt chain"
                )
        self._create_lineage_marker()
        record = _new_record(
            sequence=1 if current is None else current.sequence + 1,
            parent_checksum=None if current is None else current.checksum,
            evidence=evidence,
            request_digest=request_digest,
            target_registry_receipt_digest=registry_digest,
            target_trading_day_evidence_sequence=tde_sequence,
            target_trading_day_evidence_checksum=tde_checksum,
        )
        payload = {
            **_record_payload(
                sequence=record.sequence,
                parent_checksum=record.parent_checksum,
                evidence=record.evidence,
                request_digest=record.request_digest,
                target_registry_receipt_digest=record.target_registry_receipt_digest,
                target_trading_day_evidence_sequence=record.target_trading_day_evidence_sequence,
                target_trading_day_evidence_checksum=record.target_trading_day_evidence_checksum,
            ),
            "checksum": record.checksum,
        }
        if current is not None:
            self._atomic_replace(self.previous_path, self._read_bytes(self.path))
        self._atomic_replace(
            self.path,
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8"),
        )
        durable = self.load_required_record()
        if durable != record:
            raise Stress90OperatorContinuityError(
                "operator continuity durable receipt revalidation failed"
            )
        return durable

    def require_current_binding(
        self,
        *,
        account_identity_digest: str,
        account_epoch: str,
        canonical_runtime: str | Path,
        target_ctp_trading_day: str,
    ) -> Stress90OperatorContinuityRecord:
        current = self.load_required_record()
        account = _sha(account_identity_digest, "account identity digest")
        epoch = _sha(account_epoch, "account epoch")
        runtime, runtime_digest = _canonical_runtime(os.fspath(canonical_runtime))
        _day(target_ctp_trading_day, "target CTP trading day")
        if (
            current.evidence.account_identity_digest != account
            or current.evidence.account_epoch != epoch
            or current.evidence.canonical_runtime != runtime
            or current.evidence.canonical_runtime_digest != runtime_digest
            or current.evidence.target_ctp_trading_day != target_ctp_trading_day
        ):
            raise Stress90OperatorContinuityError(
                "operator continuity account/runtime/epoch/day binding mismatch"
            )
        return current
