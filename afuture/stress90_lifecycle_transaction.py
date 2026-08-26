"""Recoverable cross-file lifecycle commits for Stress-90 commissioning.

The generic runtime state and the independent Stress-90 policy state cannot be
atomically replaced as one filesystem object. This write-ahead record makes that
split explicit: both target payloads are durably prepared first, each state advances
only from its exact source revision, and a retry only rolls forward to the recorded
targets. Runtime construction rejects a prepared transaction; no automatic rollback
or `.prev` fallback is permitted.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from math import isclose, isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import MappingProxyType

from .directional_stress90_policy import STRESS90_POLICY
from .directional_stress90_state import (
    Stress90PolicyState,
    Stress90PolicyStateStore,
    Stress90StateIntegrityError,
    Stress90StateRecord,
    record_completed_account_day,
)
from .directional_stress90_state import (
    _state_from_payload as _stress90_state_from_payload,
)
from .directional_stress90_state import (
    _state_payload as _stress90_state_payload,
)
from .models import AccountSnapshot, ContractPosition, RuntimeMode
from .state import RuntimeState, RuntimeStateRecord, StateStore

_KIND = "afuture.stress90.lifecycle-transaction"
_SCHEMA = 9
_SHA = re.compile(r"[0-9a-f]{64}")
_LINEAGE_MARKER = b'{"kind":"afuture.stress90-lifecycle-transaction-lineage","schema_version":1}\n'
_OPERATIONS = {
    "activation",
    "reactivation",
    "account_rebase",
    "settlement_roll_forward",
    "stress90_to_execution_aligned",
}
_STATUSES = {"prepared", "committed"}
_ACCOUNT_EVIDENCE_SCOPES = {"account_snapshot", "settlement_snapshot"}
_SETTLEMENT_EVIDENCE_FIELDS = {
    "account_identity_digest",
    "trading_day",
    "deposit",
    "withdrawal",
    "cash_flow_verified",
    "previous_settlement_equity",
    "settlement_verified",
    "settlement_id",
}
_POLICY_IDENTITY_STATE_KEY = "directional_policy_identity"
_STRESS90_MARKER_FIELDS = {
    "policy_id",
    "policy_definition_digest",
    "products_manifest_digest",
    "bootstrap_seed_digest",
    "account_identity_digest",
    "operator_reason",
}
_EXECUTION_ALIGNED_MARKER_FIELDS = {
    "policy_id",
    "policy_definition_digest",
    "products_manifest_digest",
    "account_identity_digest",
    "operator_reason",
}
_MIGRATED_EXECUTION_ALIGNED_MARKER_FIELDS = {
    *_EXECUTION_ALIGNED_MARKER_FIELDS,
    "migrated_from_policy_id",
    "migrated_from_policy_definition_digest",
}

_ACTIVATION_GENERIC_MUTABLE_FIELDS = {
    "kill_reason",
    "reconciled",
    "trading_day",
    "day_start_equity",
    "equity_high_watermark",
    "metadata_verified",
    "last_account_equity",
    "last_account_trading_day",
    "last_account_deposit",
    "last_account_withdrawal",
    "last_account_cash_flow_verified",
    "last_account_settlement_id",
    "directional_daily_circuit_day",
    "strategy_states",
}
_FRESH_ACTIVATION_GENERIC_MUTABLE_FIELDS = {
    *_ACTIVATION_GENERIC_MUTABLE_FIELDS,
    "kill_switch",
    "runtime_mode",
}
_REBASE_GENERIC_MUTABLE_FIELDS = {
    *_ACTIVATION_GENERIC_MUTABLE_FIELDS,
    "last_order_id",
    "last_trade_id",
    "recent_trade_ids",
    "recent_daily_returns",
}
_MIGRATION_GENERIC_MUTABLE_FIELDS = {
    "kill_reason",
    "metadata_verified",
    "directional_daily_circuit_day",
    "strategy_states",
}
_REACTIVATION_GENERIC_MUTABLE_FIELDS = {
    *_MIGRATION_GENERIC_MUTABLE_FIELDS,
    "recent_daily_returns",
}
_SETTLEMENT_GENERIC_MUTABLE_FIELDS = {
    "kill_reason",
    "trading_day",
    "day_start_equity",
    "equity_high_watermark",
    "metadata_verified",
    "last_account_equity",
    "last_account_trading_day",
    "last_account_deposit",
    "last_account_withdrawal",
    "last_account_cash_flow_verified",
    "last_account_settlement_id",
    "directional_daily_circuit_day",
    "recent_daily_returns",
}
_ACTIVATION_POLICY_MUTABLE_FIELDS = {
    "live_inception_day",
    "live_inception_equity",
    "live_account_identity_digest",
    "live_account_epoch",
}
_REBASE_POLICY_MUTABLE_FIELDS = {
    "completed_account_wealth",
    "completed_account_high_watermark",
    "last_completed_account_day",
    "recent_daily_returns_for_adaptive_margin",
    "live_inception_day",
    "live_inception_equity",
    "live_account_identity_digest",
    "live_account_epoch",
}
_SETTLEMENT_POLICY_MUTABLE_FIELDS = {
    "completed_account_wealth",
    "completed_account_high_watermark",
    "last_completed_account_day",
    "recent_daily_returns_for_adaptive_margin",
}


class Stress90LifecycleTransactionError(RuntimeError):
    """Lifecycle state cannot be safely interpreted or advanced."""


@dataclass(frozen=True)
class Stress90LifecycleAccountTransition:
    day_start_equity: float
    equity_high_watermark: float
    verified_deposit_delta: float
    verified_withdrawal_delta: float


@dataclass(frozen=True)
class Stress90SettlementRollForwardTargets:
    completed_account_day: str
    current_ctp_trading_day: str
    completed_return: float
    partial_inception_day: bool
    generic_target: RuntimeState
    policy_target: Stress90PolicyState


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
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle transaction is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stress90LifecycleTransactionError(
                f"duplicate Stress-90 lifecycle transaction key: {key}"
            )
        result[key] = value
    return result


def _positive_int(raw: object, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise Stress90LifecycleTransactionError(f"{name} must be a positive integer")
    return raw


def _nonnegative_int(raw: object, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise Stress90LifecycleTransactionError(f"{name} must be a nonnegative integer")
    return raw


def _sha(raw: object, name: str, *, allow_empty: bool = False) -> str:
    if allow_empty and raw == "":
        return ""
    if not isinstance(raw, str) or _SHA.fullmatch(raw) is None:
        raise Stress90LifecycleTransactionError(f"{name} must be SHA-256")
    return raw


def _day(raw: object) -> str:
    from datetime import datetime

    if not isinstance(raw, str):
        raise Stress90LifecycleTransactionError("lifecycle trading day must be YYYYMMDD")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise Stress90LifecycleTransactionError("lifecycle trading day must be YYYYMMDD") from exc
    if parsed != raw:
        raise Stress90LifecycleTransactionError("lifecycle trading day must be YYYYMMDD")
    return raw


def derive_stress90_account_epoch(
    *,
    operation: str,
    operation_nonce: str,
    policy_source_checksum: str,
    account_identity_digest: str,
    trading_day: str,
) -> str:
    """Derive a lineage-bound capability epoch; an old operator nonce cannot revive it."""

    if operation not in {"activation", "reactivation", "account_rebase"}:
        raise Stress90LifecycleTransactionError(
            "account epoch derivation requires activation, reactivation or account rebase"
        )
    return _digest(
        {
            "kind": "afuture.stress90.account-epoch",
            "version": 1,
            "operation": operation,
            "operation_nonce": _sha(operation_nonce, "lifecycle operation nonce"),
            "policy_source_checksum": _sha(
                policy_source_checksum,
                "policy source checksum",
            ),
            "account_identity_digest": _sha(
                account_identity_digest,
                "account identity",
            ),
            "trading_day": _day(trading_day),
        }
    )


def _reason(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise Stress90LifecycleTransactionError("lifecycle operator reason must be non-empty")
    return raw.strip()


def _nonnegative_finite(raw: object, name: str) -> float:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not isfinite(raw)
        or float(raw) < 0.0
    ):
        raise Stress90LifecycleTransactionError(f"{name} must be finite and nonnegative")
    return float(raw)


def _finite_float(raw: object, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not isfinite(raw):
        raise Stress90LifecycleTransactionError(f"{name} must be finite")
    return float(raw)


def _boolean(raw: object, name: str) -> bool:
    if not isinstance(raw, bool):
        raise Stress90LifecycleTransactionError(f"{name} must be boolean")
    return raw


def _account_snapshot_evidence_payload(
    account: AccountSnapshot,
    *,
    account_identity_digest: str,
) -> dict[str, object]:
    try:
        account.validate()
    except ValueError as exc:
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle account snapshot is invalid"
        ) from exc
    if not account.cash_flow_verified:
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle account cash-flow evidence is unverified"
        )
    if (
        not account.settlement_verified
        or account.previous_settlement_equity is None
        or account.settlement_id is None
    ):
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle settlement evidence is unverified"
        )
    return {
        "account_identity_digest": _sha(account_identity_digest, "account identity"),
        "balance": float(account.balance),
        "equity": float(account.equity),
        "available": float(account.available),
        "margin": float(account.margin),
        "realized_pnl": float(account.realized_pnl),
        "unrealized_pnl": float(account.unrealized_pnl),
        "trading_day": _day(account.trading_day),
        "deposit": float(account.deposit),
        "withdrawal": float(account.withdrawal),
        "cash_flow_verified": account.cash_flow_verified,
        "previous_settlement_equity": (
            None
            if account.previous_settlement_equity is None
            else float(account.previous_settlement_equity)
        ),
        "settlement_verified": account.settlement_verified,
        "settlement_id": account.settlement_id,
    }


def _scoped_account_evidence_payload(
    evidence: Mapping[str, object],
    *,
    scope: str,
) -> dict[str, object]:
    if scope == "account_snapshot":
        return dict(evidence)
    if scope == "settlement_snapshot":
        return {key: evidence[key] for key in sorted(_SETTLEMENT_EVIDENCE_FIELDS)}
    raise Stress90LifecycleTransactionError("Stress-90 lifecycle account evidence scope is invalid")


def stress90_lifecycle_account_transition(
    *,
    operation: str,
    generic_source: RuntimeState | None,
    account_snapshot: AccountSnapshot,
    account_switched: bool,
) -> Stress90LifecycleAccountTransition:
    """Compute hard baselines from exact CTP settlement and cumulative cash flow."""

    if operation not in {
        "activation",
        "reactivation",
        "account_rebase",
        "settlement_roll_forward",
    }:
        raise Stress90LifecycleTransactionError(
            "account transition requires an account lifecycle operation"
        )
    _account_snapshot_evidence_payload(
        account_snapshot,
        account_identity_digest="0" * 64,
    )
    settlement_raw = account_snapshot.previous_settlement_equity
    settlement_id = account_snapshot.settlement_id
    if settlement_raw is None or settlement_id is None:
        raise Stress90LifecycleTransactionError("lifecycle account settlement evidence is missing")
    day = _day(account_snapshot.trading_day)
    deposit = float(account_snapshot.deposit)
    withdrawal = float(account_snapshot.withdrawal)
    settlement = float(settlement_raw)
    equity = float(account_snapshot.equity)
    cumulative_day_start = settlement + deposit - withdrawal
    if not isfinite(cumulative_day_start) or cumulative_day_start <= 0.0:
        raise Stress90LifecycleTransactionError(
            "lifecycle cumulative cash-flow daily baseline is invalid"
        )
    if generic_source is None:
        if operation != "activation":
            raise Stress90LifecycleTransactionError(
                "reactivation/account rebase requires an existing generic source"
            )
        return Stress90LifecycleAccountTransition(
            cumulative_day_start,
            equity,
            deposit,
            withdrawal,
        )

    source_day = str(generic_source.trading_day or "")
    source_account_day = str(generic_source.last_account_trading_day or "")
    for value in (source_day, source_account_day):
        if value and _day(value) > day:
            raise Stress90LifecycleTransactionError(
                "lifecycle account transition trading day moved backward"
            )
    if not source_day or not source_account_day or source_day != source_account_day:
        raise Stress90LifecycleTransactionError(
            "lifecycle source day identity is missing or inconsistent"
        )
    source_settlement_id = generic_source.last_account_settlement_id
    if (
        source_settlement_id is None
        or not generic_source.last_account_cash_flow_verified
        or source_settlement_id < 0
    ):
        raise Stress90LifecycleTransactionError(
            "lifecycle source settlement/cash-flow evidence is unverified"
        )
    if (
        not account_switched
        and source_account_day != day
        and operation not in {"account_rebase", "settlement_roll_forward"}
    ):
        raise Stress90LifecycleTransactionError(
            "same-account lifecycle transition requires the same CTP trading day; "
            "a complete settlement/cash-flow high-watermark backfill is not available"
        )
    if operation == "reactivation" and account_switched:
        raise Stress90LifecycleTransactionError(
            "Stress-90 reactivation cannot switch account identity; use account rebase"
        )

    if operation == "settlement_roll_forward":
        if account_switched:
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward cannot switch account identity"
            )
        if source_account_day >= day:
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward must advance the CTP trading day"
            )
        if deposit != 0.0 or withdrawal != 0.0:
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward current-day cash flow requires explicit rebase"
            )
        source_hwm = float(generic_source.equity_high_watermark or 0.0)
        if source_hwm <= 0.0:
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward hard drawdown baseline is missing"
            )
        return Stress90LifecycleAccountTransition(
            settlement,
            max(source_hwm, settlement, equity),
            0.0,
            0.0,
        )

    same_account_day = source_account_day == day
    if (
        operation == "account_rebase"
        and not account_switched
        and not same_account_day
        and deposit == 0.0
        and withdrawal == 0.0
    ):
        raise Stress90LifecycleTransactionError(
            "cross-day account rebase requires current-day cash flow; "
            "use settlement roll-forward otherwise"
        )
    if same_account_day and not account_switched:
        source_prebalance = (
            float(generic_source.day_start_equity)
            - float(generic_source.last_account_deposit)
            + float(generic_source.last_account_withdrawal)
        )
        evidence_settlement_id = settlement_id
        if (
            source_settlement_id != evidence_settlement_id
            or not isfinite(source_prebalance)
            or source_prebalance <= 0.0
            or not stress90_settlement_amounts_match(source_prebalance, settlement)
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle same-day settlement identity/prebalance changed"
            )
    if account_switched:
        if operation != "account_rebase":
            raise Stress90LifecycleTransactionError("activation cannot switch account identity")
        deposit_delta = 0.0
        withdrawal_delta = 0.0
    elif same_account_day:
        deposit_delta = deposit - float(generic_source.last_account_deposit)
        withdrawal_delta = withdrawal - float(generic_source.last_account_withdrawal)
    else:
        deposit_delta = deposit
        withdrawal_delta = withdrawal
    if deposit_delta < 0.0 or withdrawal_delta < 0.0:
        raise Stress90LifecycleTransactionError(
            "lifecycle account cash-flow evidence moved backward"
        )

    source_hwm = float(generic_source.equity_high_watermark or 0.0)
    if operation in {"activation", "reactivation"} and source_day == day:
        if deposit_delta != 0.0 or withdrawal_delta != 0.0:
            raise Stress90LifecycleTransactionError(
                "same-day activation/reactivation cash flow changed; explicit account rebase "
                "is required"
            )
        day_start = float(generic_source.day_start_equity or 0.0)
        if day_start <= 0.0:
            raise Stress90LifecycleTransactionError(
                "same-day activation hard daily-loss baseline is missing"
            )
        high_watermark = max(source_hwm, equity)
    elif operation in {"activation", "reactivation"}:
        day_start = cumulative_day_start
        high_watermark = max(equity, source_hwm + deposit_delta - withdrawal_delta)
    elif account_switched:
        day_start = cumulative_day_start
        high_watermark = equity
    else:
        if source_hwm <= 0.0:
            raise Stress90LifecycleTransactionError(
                "account rebase hard drawdown baseline is missing"
            )
        day_start = cumulative_day_start
        rebase_source_hwm = max(source_hwm, settlement) if not same_account_day else source_hwm
        high_watermark = max(
            equity,
            rebase_source_hwm + deposit_delta - withdrawal_delta,
        )
    if not isfinite(high_watermark) or high_watermark <= 0.0:
        raise Stress90LifecycleTransactionError("lifecycle hard drawdown baseline is invalid")
    return Stress90LifecycleAccountTransition(
        day_start,
        high_watermark,
        deposit_delta,
        withdrawal_delta,
    )


def stress90_settlement_amounts_match(left: object, right: object) -> bool:
    """Compare positive finite CTP settlement amounts with one shared tolerance."""

    if (
        isinstance(left, bool)
        or not isinstance(left, (int, float))
        or isinstance(right, bool)
        or not isinstance(right, (int, float))
    ):
        return False
    lhs = float(left)
    rhs = float(right)
    return bool(
        isfinite(lhs)
        and isfinite(rhs)
        and lhs > 0.0
        and rhs > 0.0
        and isclose(lhs, rhs, rel_tol=1e-12, abs_tol=1e-8)
    )


def build_stress90_settlement_roll_forward_targets(
    generic_source: RuntimeState,
    policy_source: Stress90PolicyState,
    account_snapshot: AccountSnapshot,
) -> Stress90SettlementRollForwardTargets:
    """Build one zero-cash-flow, HALTED settlement target without order authority."""

    source_day = _day(generic_source.trading_day)
    current_day = _day(account_snapshot.trading_day)
    if generic_source.last_account_trading_day != source_day:
        raise Stress90LifecycleTransactionError(
            "settlement roll-forward source day identity is inconsistent"
        )
    transition = stress90_lifecycle_account_transition(
        operation="settlement_roll_forward",
        generic_source=generic_source,
        account_snapshot=account_snapshot,
        account_switched=False,
    )
    inception_day = policy_source.live_inception_day
    if inception_day is None or source_day < inception_day:
        raise Stress90LifecycleTransactionError(
            "settlement roll-forward live inception path is missing or inconsistent"
        )
    settlement = account_snapshot.previous_settlement_equity
    settlement_id = account_snapshot.settlement_id
    if settlement is None or settlement_id is None:
        raise Stress90LifecycleTransactionError(
            "settlement roll-forward settlement evidence is missing"
        )
    inception_equity = policy_source.live_inception_equity
    partial_inception_day = source_day == inception_day
    completed_return_denominator = (
        inception_equity if partial_inception_day else generic_source.day_start_equity
    )
    if completed_return_denominator is None:
        raise Stress90LifecycleTransactionError(
            "settlement roll-forward live inception equity is missing"
        )
    completed_return = float(settlement) / float(completed_return_denominator) - 1.0
    if not isfinite(completed_return) or completed_return <= -1.0:
        raise Stress90LifecycleTransactionError(
            "settlement roll-forward completed account return is invalid"
        )
    try:
        policy_target = record_completed_account_day(
            policy_source,
            source_day,
            completed_return,
            include_adaptive_margin=not partial_inception_day,
        )
    except Stress90StateIntegrityError as exc:
        raise Stress90LifecycleTransactionError(
            "settlement roll-forward policy account path is discontinuous"
        ) from exc
    recent_returns = list(generic_source.recent_daily_returns)
    if not partial_inception_day:
        recent_returns = [*recent_returns[-1:], completed_return]
    generic_target = replace(
        generic_source,
        kill_reason=("Stress-90 settlement rolled forward; doctor/Shadow gates remain required"),
        trading_day=current_day,
        day_start_equity=transition.day_start_equity,
        equity_high_watermark=transition.equity_high_watermark,
        metadata_verified=False,
        last_account_equity=float(account_snapshot.equity),
        last_account_trading_day=current_day,
        last_account_deposit=float(account_snapshot.deposit),
        last_account_withdrawal=float(account_snapshot.withdrawal),
        last_account_cash_flow_verified=bool(account_snapshot.cash_flow_verified),
        last_account_settlement_id=settlement_id,
        directional_daily_circuit_day="",
        recent_daily_returns=recent_returns,
    )
    return Stress90SettlementRollForwardTargets(
        completed_account_day=source_day,
        current_ctp_trading_day=current_day,
        completed_return=completed_return,
        partial_inception_day=partial_inception_day,
        generic_target=generic_target,
        policy_target=policy_target,
    )


def _require_migration_account_continuity(
    generic_source: RuntimeState,
    account_snapshot: AccountSnapshot,
) -> None:
    """Migration cannot consume cash-flow or trading-day rollover authority."""

    if (
        generic_source.trading_day != account_snapshot.trading_day
        or generic_source.last_account_trading_day != account_snapshot.trading_day
    ):
        raise Stress90LifecycleTransactionError(
            "policy migration requires same-day account continuity; process rollover or rebase first"
        )
    transition = stress90_lifecycle_account_transition(
        operation="activation",
        generic_source=generic_source,
        account_snapshot=account_snapshot,
        account_switched=False,
    )
    if transition.day_start_equity != generic_source.day_start_equity:
        raise Stress90LifecycleTransactionError(
            "policy migration account baseline changed; explicit rebase is required"
        )
    if (
        transition.equity_high_watermark != generic_source.equity_high_watermark
        or generic_source.last_account_equity != account_snapshot.equity
        or generic_source.last_account_deposit != account_snapshot.deposit
        or generic_source.last_account_withdrawal != account_snapshot.withdrawal
        or generic_source.last_account_settlement_id != account_snapshot.settlement_id
    ):
        raise Stress90LifecycleTransactionError(
            "policy migration account snapshot/high-watermark continuity changed"
        )


def _account_snapshot_from_evidence(evidence: Mapping[str, object]) -> AccountSnapshot:
    account_identity_digest = _sha(
        evidence.get("account_identity_digest"),
        "account identity",
    )
    return _decode_account_snapshot_evidence(
        evidence,
        account_identity_digest=account_identity_digest,
    )


def _decode_account_snapshot_evidence(
    raw: object,
    *,
    account_identity_digest: str,
) -> AccountSnapshot:
    fields = {
        "account_identity_digest",
        "balance",
        "equity",
        "available",
        "margin",
        "realized_pnl",
        "unrealized_pnl",
        "trading_day",
        "deposit",
        "withdrawal",
        "cash_flow_verified",
        "previous_settlement_equity",
        "settlement_verified",
        "settlement_id",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle account evidence fields are invalid"
        )
    if _sha(raw["account_identity_digest"], "account identity") != account_identity_digest:
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle account evidence identity mismatch"
        )
    previous_settlement_raw = raw["previous_settlement_equity"]
    previous_settlement_equity = (
        None
        if previous_settlement_raw is None
        else _finite_float(previous_settlement_raw, "previous settlement equity")
    )
    if previous_settlement_equity is not None and previous_settlement_equity <= 0.0:
        raise Stress90LifecycleTransactionError("Stress-90 lifecycle account evidence is invalid")
    settlement_id_raw = raw["settlement_id"]
    settlement_id = (
        None if settlement_id_raw is None else _nonnegative_int(settlement_id_raw, "settlement id")
    )
    account = AccountSnapshot(
        balance=_finite_float(raw["balance"], "balance"),
        equity=_finite_float(raw["equity"], "equity"),
        available=_nonnegative_finite(raw["available"], "available"),
        margin=_nonnegative_finite(raw["margin"], "margin"),
        realized_pnl=_finite_float(raw["realized_pnl"], "realized pnl"),
        unrealized_pnl=_finite_float(raw["unrealized_pnl"], "unrealized pnl"),
        trading_day=_day(raw["trading_day"]),
        deposit=_nonnegative_finite(raw["deposit"], "deposit"),
        withdrawal=_nonnegative_finite(raw["withdrawal"], "withdrawal"),
        cash_flow_verified=_boolean(raw["cash_flow_verified"], "cash-flow verification"),
        previous_settlement_equity=previous_settlement_equity,
        settlement_verified=_boolean(raw["settlement_verified"], "settlement verification"),
        settlement_id=settlement_id,
    )
    _account_snapshot_evidence_payload(
        account,
        account_identity_digest=account_identity_digest,
    )
    return account


def stress90_lifecycle_account_evidence_digest(
    account: AccountSnapshot,
    *,
    account_identity_digest: str,
    scope: str = "account_snapshot",
) -> str:
    """Digest the immutable Broker evidence used by one lifecycle operation."""

    evidence = _account_snapshot_evidence_payload(
        account,
        account_identity_digest=account_identity_digest,
    )
    return _digest(
        _scoped_account_evidence_payload(
            evidence,
            scope=scope,
        )
    )


@dataclass(frozen=True)
class Stress90LifecycleTransaction:
    transaction_id: str
    operation_nonce: str
    operation: str
    operator_reason: str
    status: str
    trading_day: str
    source_account_identity_digest: str
    source_account_epoch: str
    account_identity_digest: str
    account_evidence_scope: str
    account_evidence: Mapping[str, object]
    account_evidence_digest: str
    account_day_continuity_digest: str
    account_day_continuity_source_day: str
    verified_deposit_delta: float
    verified_withdrawal_delta: float
    generic_source_sequence: int
    generic_source_checksum: str
    policy_source_sequence: int
    policy_source_checksum: str
    generic_target: RuntimeState
    policy_target: Stress90PolicyState


@dataclass(frozen=True)
class _TransactionRecord:
    transaction: Stress90LifecycleTransaction
    sequence: int
    checksum: str


def _transaction_payload(transaction: Stress90LifecycleTransaction) -> dict[str, object]:
    return {
        "transaction_id": transaction.transaction_id,
        "operation_nonce": transaction.operation_nonce,
        "operation": transaction.operation,
        "operator_reason": transaction.operator_reason,
        "status": transaction.status,
        "trading_day": transaction.trading_day,
        "source_account_identity_digest": transaction.source_account_identity_digest,
        "source_account_epoch": transaction.source_account_epoch,
        "account_identity_digest": transaction.account_identity_digest,
        "account_evidence_scope": transaction.account_evidence_scope,
        "account_evidence": dict(transaction.account_evidence),
        "account_evidence_digest": transaction.account_evidence_digest,
        "account_day_continuity_digest": transaction.account_day_continuity_digest,
        "account_day_continuity_source_day": (transaction.account_day_continuity_source_day),
        "verified_deposit_delta": transaction.verified_deposit_delta,
        "verified_withdrawal_delta": transaction.verified_withdrawal_delta,
        "generic_source_sequence": transaction.generic_source_sequence,
        "generic_source_checksum": transaction.generic_source_checksum,
        "policy_source_sequence": transaction.policy_source_sequence,
        "policy_source_checksum": transaction.policy_source_checksum,
        "generic_target": vars(transaction.generic_target),
        "policy_target": _stress90_state_payload(transaction.policy_target),
    }


def _decode_transaction(raw: object) -> Stress90LifecycleTransaction:
    fields = {
        "transaction_id",
        "operation_nonce",
        "operation",
        "operator_reason",
        "status",
        "trading_day",
        "source_account_identity_digest",
        "source_account_epoch",
        "account_identity_digest",
        "account_evidence_scope",
        "account_evidence",
        "account_evidence_digest",
        "account_day_continuity_digest",
        "account_day_continuity_source_day",
        "verified_deposit_delta",
        "verified_withdrawal_delta",
        "generic_source_sequence",
        "generic_source_checksum",
        "policy_source_sequence",
        "policy_source_checksum",
        "generic_target",
        "policy_target",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle transaction fields are invalid"
        )
    operation = raw["operation"]
    status = raw["status"]
    if (
        not isinstance(operation, str)
        or operation not in _OPERATIONS
        or not isinstance(status, str)
        or status not in _STATUSES
    ):
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle transaction operation/status is invalid"
        )
    generic_sequence = _nonnegative_int(raw["generic_source_sequence"], "generic source sequence")
    generic_checksum = _sha(
        raw["generic_source_checksum"],
        "generic source checksum",
        allow_empty=generic_sequence == 0,
    )
    if (generic_sequence == 0) != (generic_checksum == ""):
        raise Stress90LifecycleTransactionError("generic lifecycle source revision is invalid")
    generic_raw = raw["generic_target"]
    if not isinstance(generic_raw, dict):
        raise Stress90LifecycleTransactionError("generic lifecycle target is invalid")
    try:
        generic_target = StateStore._state_from_payload(generic_raw)
    except (TypeError, ValueError, Stress90StateIntegrityError) as exc:
        raise Stress90LifecycleTransactionError("generic lifecycle target is invalid") from exc
    if generic_target.runtime_mode != RuntimeMode.HALTED.value or not generic_target.kill_switch:
        raise Stress90LifecycleTransactionError(
            "lifecycle target must preserve HALTED state and kill switch"
        )
    policy_raw = raw["policy_target"]
    if not isinstance(policy_raw, Mapping):
        raise Stress90LifecycleTransactionError("policy lifecycle target is invalid")
    try:
        policy_target = _stress90_state_from_payload(policy_raw, STRESS90_POLICY)
    except (TypeError, ValueError, Stress90StateIntegrityError) as exc:
        raise Stress90LifecycleTransactionError("policy lifecycle target is invalid") from exc
    account_evidence_scope = raw["account_evidence_scope"]
    if (
        not isinstance(account_evidence_scope, str)
        or account_evidence_scope not in _ACCOUNT_EVIDENCE_SCOPES
    ):
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle account evidence scope is invalid"
        )
    account_identity = _sha(raw["account_identity_digest"], "account identity")
    account_snapshot = _decode_account_snapshot_evidence(
        raw["account_evidence"],
        account_identity_digest=account_identity,
    )
    account_evidence = _account_snapshot_evidence_payload(
        account_snapshot,
        account_identity_digest=account_identity,
    )
    account_evidence_digest = _sha(raw["account_evidence_digest"], "account evidence digest")
    if account_evidence_digest != _digest(
        _scoped_account_evidence_payload(
            account_evidence,
            scope=str(account_evidence_scope),
        )
    ):
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle account evidence digest mismatch"
        )
    transaction = Stress90LifecycleTransaction(
        transaction_id=_sha(raw["transaction_id"], "lifecycle transaction identity"),
        operation_nonce=_sha(raw["operation_nonce"], "lifecycle operation nonce"),
        operation=str(operation),
        operator_reason=_reason(raw["operator_reason"]),
        status=str(status),
        trading_day=_day(raw["trading_day"]),
        source_account_identity_digest=_sha(
            raw["source_account_identity_digest"],
            "source account identity",
        ),
        source_account_epoch=_sha(
            raw["source_account_epoch"],
            "source account epoch",
            allow_empty=True,
        ),
        account_identity_digest=account_identity,
        account_evidence_scope=str(account_evidence_scope),
        account_evidence=MappingProxyType(account_evidence),
        account_evidence_digest=account_evidence_digest,
        account_day_continuity_digest=_sha(
            raw["account_day_continuity_digest"],
            "account-day continuity digest",
            allow_empty=operation != "settlement_roll_forward",
        ),
        account_day_continuity_source_day=(
            ""
            if raw["account_day_continuity_source_day"] == ""
            else _day(raw["account_day_continuity_source_day"])
        ),
        verified_deposit_delta=_nonnegative_finite(
            raw["verified_deposit_delta"], "verified deposit delta"
        ),
        verified_withdrawal_delta=_nonnegative_finite(
            raw["verified_withdrawal_delta"], "verified withdrawal delta"
        ),
        generic_source_sequence=generic_sequence,
        generic_source_checksum=generic_checksum,
        policy_source_sequence=_positive_int(
            raw["policy_source_sequence"], "policy source sequence"
        ),
        policy_source_checksum=_sha(raw["policy_source_checksum"], "policy source checksum"),
        generic_target=generic_target,
        policy_target=policy_target,
    )
    _validate_operation_invariants(transaction)
    identity_source = _transaction_payload(replace(transaction, status="prepared"))
    identity_source.pop("transaction_id")
    if transaction.transaction_id != _digest(identity_source):
        raise Stress90LifecycleTransactionError("Stress-90 lifecycle transaction identity mismatch")
    return transaction


def _validate_operation_invariants(transaction: Stress90LifecycleTransaction) -> None:
    generic = transaction.generic_target
    policy = transaction.policy_target
    account_identity = transaction.account_identity_digest
    if generic.runtime_mode != RuntimeMode.HALTED.value or not generic.kill_switch:
        raise Stress90LifecycleTransactionError(
            "lifecycle target must preserve HALTED state and kill switch"
        )
    if generic.directional_daily_circuit_day:
        raise Stress90LifecycleTransactionError(
            "lifecycle target must revoke stale daily-circuit recovery authority"
        )
    try:
        locally_flat = not any(not ContractPosition(**item).empty for item in generic.positions)
    except (TypeError, ValueError) as exc:
        raise Stress90LifecycleTransactionError(
            "lifecycle generic target positions are invalid"
        ) from exc
    if not generic.reconciled or (
        transaction.operation != "settlement_roll_forward" and not locally_flat
    ):
        raise Stress90LifecycleTransactionError(
            "lifecycle generic target must be reconciled and locally flat"
        )
    if (
        policy.policy_id != STRESS90_POLICY.policy_id
        or policy.policy_definition_digest != STRESS90_POLICY.policy_definition_digest
        or policy.products_manifest_digest != STRESS90_POLICY.products_manifest_digest
    ):
        raise Stress90LifecycleTransactionError(
            "lifecycle policy target definition identity mismatch"
        )
    if policy.live_account_identity_digest != account_identity:
        raise Stress90LifecycleTransactionError("lifecycle cross-file account identity mismatch")

    marker = generic.strategy_states.get(_POLICY_IDENTITY_STATE_KEY)
    if not isinstance(marker, dict):
        raise Stress90LifecycleTransactionError(
            "lifecycle generic policy identity marker is invalid"
        )
    if marker.get("account_identity_digest") != account_identity:
        raise Stress90LifecycleTransactionError(
            "lifecycle generic/account policy identity mismatch"
        )
    if bool(transaction.account_day_continuity_digest) != bool(
        transaction.account_day_continuity_source_day
    ):
        raise Stress90LifecycleTransactionError(
            "lifecycle account-day continuity identity is incomplete"
        )
    if (
        transaction.account_day_continuity_source_day
        and transaction.account_day_continuity_source_day >= transaction.trading_day
    ):
        raise Stress90LifecycleTransactionError("lifecycle account-day continuity did not advance")

    if transaction.operation == "settlement_roll_forward":
        if (
            not transaction.source_account_epoch
            or policy.live_account_epoch != transaction.source_account_epoch
            or transaction.source_account_identity_digest != account_identity
            or not transaction.account_day_continuity_digest
            or not transaction.account_day_continuity_source_day
            or transaction.account_day_continuity_source_day >= transaction.trading_day
            or transaction.verified_deposit_delta != 0.0
            or transaction.verified_withdrawal_delta != 0.0
        ):
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward account lineage/continuity invariant mismatch"
            )
        if (
            set(marker) != _STRESS90_MARKER_FIELDS
            or marker.get("policy_id") != STRESS90_POLICY.policy_id
            or marker.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
            or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
            or marker.get("bootstrap_seed_digest") != policy.bootstrap_seed_digest
        ):
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward cross-file policy identity mismatch"
            )
        evidence = transaction.account_evidence
        evidence_day = str(evidence["trading_day"])
        evidence_equity = _finite_float(evidence["equity"], "account evidence equity")
        evidence_deposit = _nonnegative_finite(evidence["deposit"], "account evidence deposit")
        evidence_withdrawal = _nonnegative_finite(
            evidence["withdrawal"], "account evidence withdrawal"
        )
        evidence_settlement = _nonnegative_finite(
            evidence["previous_settlement_equity"],
            "account evidence previous settlement equity",
        )
        evidence_settlement_id = _nonnegative_int(
            evidence["settlement_id"], "account evidence settlement identity"
        )
        if evidence_deposit != 0.0 or evidence_withdrawal != 0.0:
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward current-day cash flow requires explicit rebase"
            )
        if (
            evidence_day != transaction.trading_day
            or generic.trading_day != evidence_day
            or generic.last_account_trading_day != evidence_day
            or generic.last_account_equity != evidence_equity
            or generic.last_account_deposit != 0.0
            or generic.last_account_withdrawal != 0.0
            or generic.last_account_cash_flow_verified is not True
            or generic.last_account_settlement_id != evidence_settlement_id
            or generic.day_start_equity != evidence_settlement
            or policy.live_inception_day is None
            or policy.live_inception_day >= transaction.trading_day
        ):
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward target/account evidence invariant mismatch"
            )
        return

    if transaction.operation not in {"account_rebase", "settlement_roll_forward"} and (
        transaction.account_day_continuity_digest or transaction.account_day_continuity_source_day
    ):
        raise Stress90LifecycleTransactionError(
            "non-settlement lifecycle transaction contains continuity evidence"
        )

    if transaction.operation in {"activation", "reactivation", "account_rebase"}:
        if transaction.operation == "activation" and transaction.source_account_epoch:
            raise Stress90LifecycleTransactionError(
                "activation source account epoch must be unbound"
            )
        if (
            transaction.operation in {"reactivation", "account_rebase"}
            and not transaction.source_account_epoch
        ):
            raise Stress90LifecycleTransactionError(
                "reactivation/account rebase source account epoch is missing"
            )
        if (
            set(marker) != _STRESS90_MARKER_FIELDS
            or marker.get("policy_id") != STRESS90_POLICY.policy_id
            or marker.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
            or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
            or marker.get("bootstrap_seed_digest") != policy.bootstrap_seed_digest
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle Stress-90 cross-file policy identity mismatch"
            )
        expected_account_epoch = derive_stress90_account_epoch(
            operation=transaction.operation,
            operation_nonce=transaction.operation_nonce,
            policy_source_checksum=transaction.policy_source_checksum,
            account_identity_digest=transaction.account_identity_digest,
            trading_day=transaction.trading_day,
        )
        if policy.live_account_epoch != expected_account_epoch:
            raise Stress90LifecycleTransactionError(
                "lifecycle Stress-90 account epoch lineage mismatch"
            )
        if transaction.source_account_epoch == policy.live_account_epoch:
            raise Stress90LifecycleTransactionError("lifecycle account epoch did not advance")
        evidence = transaction.account_evidence
        evidence_day = str(evidence["trading_day"])
        evidence_equity = _finite_float(evidence["equity"], "account evidence equity")
        evidence_deposit = _nonnegative_finite(evidence["deposit"], "account evidence deposit")
        evidence_withdrawal = _nonnegative_finite(
            evidence["withdrawal"], "account evidence withdrawal"
        )
        evidence_settlement = _nonnegative_finite(
            evidence["previous_settlement_equity"],
            "account evidence previous settlement equity",
        )
        evidence_cash_flow_verified = _boolean(
            evidence["cash_flow_verified"],
            "account evidence cash-flow verification",
        )
        evidence_settlement_id = _nonnegative_int(
            evidence["settlement_id"], "account evidence settlement identity"
        )
        if (
            evidence_day != transaction.trading_day
            or generic.trading_day != evidence_day
            or generic.last_account_trading_day != evidence_day
            or generic.last_account_equity != evidence_equity
            or generic.last_account_deposit != evidence_deposit
            or generic.last_account_withdrawal != evidence_withdrawal
            or generic.last_account_cash_flow_verified is not evidence_cash_flow_verified
            or generic.last_account_settlement_id != evidence_settlement_id
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle generic target does not match exact account snapshot"
            )
        if transaction.operation == "account_rebase" and generic.day_start_equity != (
            evidence_settlement + evidence_deposit - evidence_withdrawal
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle account rebase cumulative daily-loss baseline mismatch"
            )
        if transaction.operation in {"reactivation", "account_rebase"} and (
            generic.recent_daily_returns
        ):
            raise Stress90LifecycleTransactionError(
                f"lifecycle {transaction.operation} must clear generic adaptive-margin returns"
            )
        if (
            transaction.operation == "activation"
            and policy.live_inception_day != transaction.trading_day
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle activation must initialize the authoritative live inception day"
            )
        if transaction.operation in {"reactivation", "account_rebase"} and (
            policy.live_inception_day != transaction.trading_day
            or policy.live_inception_equity != evidence_equity
            or policy.completed_account_wealth != 1.0
            or policy.completed_account_high_watermark != 1.0
            or policy.last_completed_account_day is not None
            or policy.recent_daily_returns_for_adaptive_margin
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle account rebase target invariant mismatch"
            )
        if transaction.operation == "activation" and (
            policy.live_inception_equity != evidence_equity
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle activation inception equity mismatch"
            )
        if transaction.operation == "reactivation" and (
            transaction.source_account_identity_digest != account_identity
            or transaction.verified_deposit_delta != 0.0
            or transaction.verified_withdrawal_delta != 0.0
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle reactivation account continuity invariant mismatch"
            )
        if (
            transaction.operation == "account_rebase"
            and transaction.source_account_identity_digest == account_identity
            and transaction.verified_deposit_delta == 0.0
            and transaction.verified_withdrawal_delta == 0.0
        ):
            raise Stress90LifecycleTransactionError(
                "same-account rebase requires verified external cash flow"
            )
        if (
            transaction.operation == "account_rebase"
            and transaction.source_account_identity_digest != account_identity
            and (generic.last_order_id or generic.last_trade_id or generic.recent_trade_ids)
        ):
            raise Stress90LifecycleTransactionError(
                "account switch rebase must clear old account trade identities"
            )
        if (
            transaction.operation in {"activation", "reactivation"}
            and marker.get("operator_reason") != transaction.operator_reason
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle activation/reactivation operator reason identity mismatch"
            )
        return

    if (
        set(marker) != _MIGRATED_EXECUTION_ALIGNED_MARKER_FIELDS
        or marker.get("policy_id") != "execution_aligned"
        or marker.get("policy_definition_digest") != ""
        or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
        or marker.get("migrated_from_policy_id") != STRESS90_POLICY.policy_id
        or marker.get("migrated_from_policy_definition_digest")
        != STRESS90_POLICY.policy_definition_digest
        or marker.get("operator_reason") != transaction.operator_reason
    ):
        raise Stress90LifecycleTransactionError(
            "lifecycle migration cross-file policy identity mismatch"
        )
    if transaction.source_account_identity_digest != account_identity:
        raise Stress90LifecycleTransactionError(
            "lifecycle migration cannot change account identity"
        )
    if (
        not transaction.source_account_epoch
        or transaction.source_account_epoch != policy.live_account_epoch
    ):
        raise Stress90LifecycleTransactionError("lifecycle migration source account epoch mismatch")
    _require_migration_account_continuity(
        generic,
        _account_snapshot_from_evidence(transaction.account_evidence),
    )


def _validate_begin_source_invariants(
    *,
    operation: str,
    generic_source: RuntimeStateRecord | None,
    policy_source: Stress90StateRecord,
    policy_target: Stress90PolicyState,
    account_identity_digest: str,
    trading_day: str,
) -> str:
    source_policy = policy_source.state
    if (
        source_policy.policy_id != STRESS90_POLICY.policy_id
        or source_policy.policy_definition_digest != STRESS90_POLICY.policy_definition_digest
        or source_policy.products_manifest_digest != STRESS90_POLICY.products_manifest_digest
        or source_policy.bootstrap_seed_digest != policy_target.bootstrap_seed_digest
    ):
        raise Stress90LifecycleTransactionError(
            "lifecycle source policy definition/bootstrap identity mismatch"
        )
    policy_source_days = tuple(
        _day(day)
        for day in (
            source_policy.last_completed_target_day,
            source_policy.last_completed_account_day,
            source_policy.live_inception_day,
        )
        if day
    )
    if any(day > trading_day for day in policy_source_days):
        raise Stress90LifecycleTransactionError(
            "lifecycle authoritative trading day moved backward"
        )
    if operation == "activation" and generic_source is None:
        if not _pristine_unbound_account_path(source_policy):
            raise Stress90LifecycleTransactionError(
                "fresh activation requires a pristine, unbound account path"
            )
        return account_identity_digest
    if generic_source is None:
        raise Stress90LifecycleTransactionError(
            f"{operation} requires an existing generic runtime source"
        )

    generic = generic_source.state
    authoritative_source_days = tuple(
        _day(day)
        for day in (
            generic.trading_day,
            generic.last_account_trading_day,
        )
        if day
    )
    if any(day > trading_day for day in authoritative_source_days):
        raise Stress90LifecycleTransactionError(
            "lifecycle authoritative trading day moved backward"
        )
    if (
        generic.runtime_mode != RuntimeMode.HALTED.value
        or not generic.kill_switch
        or (operation != "activation" and not generic.reconciled)
    ):
        raise Stress90LifecycleTransactionError(
            "lifecycle source is not HALTED/reconciled for the requested operation"
        )
    try:
        if operation != "settlement_roll_forward" and any(
            not ContractPosition(**item).empty for item in generic.positions
        ):
            raise Stress90LifecycleTransactionError("lifecycle source must be locally flat")
    except (TypeError, ValueError) as exc:
        raise Stress90LifecycleTransactionError(
            "lifecycle generic source positions are invalid"
        ) from exc

    marker = generic.strategy_states.get(_POLICY_IDENTITY_STATE_KEY)
    if operation == "activation" and marker is None:
        raise Stress90LifecycleTransactionError(
            "an existing generic runtime requires an explicit execution-aligned identity "
            "before Stress-90 activation; markerless production state cannot be relabeled"
        )
    if not isinstance(marker, dict):
        raise Stress90LifecycleTransactionError(
            "lifecycle source policy identity marker is invalid"
        )

    if operation == "reactivation":
        if (
            set(marker) != _MIGRATED_EXECUTION_ALIGNED_MARKER_FIELDS
            or marker.get("policy_id") != "execution_aligned"
            or marker.get("policy_definition_digest") != ""
            or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
            or marker.get("migrated_from_policy_id") != STRESS90_POLICY.policy_id
            or marker.get("migrated_from_policy_definition_digest")
            != STRESS90_POLICY.policy_definition_digest
            or marker.get("account_identity_digest") != account_identity_digest
            or source_policy.live_account_identity_digest != account_identity_digest
            or not source_policy.live_account_epoch
        ):
            raise Stress90LifecycleTransactionError(
                "reactivation requires the exact migrated execution-aligned provenance, "
                "Stress-90 policy state and account lineage"
            )
        if generic.trading_day != trading_day or generic.last_account_trading_day != trading_day:
            raise Stress90LifecycleTransactionError(
                "reactivation requires same-day generic account continuity"
            )
        return account_identity_digest

    if operation == "activation" and marker.get("policy_id") == "execution_aligned":
        provenance_fields = {
            "migrated_from_policy_id",
            "migrated_from_policy_definition_digest",
        }
        if provenance_fields.intersection(marker):
            if (
                marker.get("migrated_from_policy_id") == STRESS90_POLICY.policy_id
                and marker.get("migrated_from_policy_definition_digest")
                == STRESS90_POLICY.policy_definition_digest
            ):
                raise Stress90LifecycleTransactionError(
                    "Stress-90 reactivation after migration requires a fresh "
                    "bootstrap/account rebase"
                )
            raise Stress90LifecycleTransactionError(
                "activation source migration provenance identity mismatch"
            )
        if (
            set(marker) != _EXECUTION_ALIGNED_MARKER_FIELDS
            or marker.get("policy_definition_digest") != ""
            or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
            or marker.get("account_identity_digest") != account_identity_digest
            or source_policy.live_account_identity_digest not in {None, account_identity_digest}
        ):
            raise Stress90LifecycleTransactionError(
                "activation source execution-aligned identity mismatch"
            )
        if not _pristine_unbound_account_path(source_policy):
            raise Stress90LifecycleTransactionError(
                "execution-aligned activation requires a pristine account path"
            )
        return account_identity_digest

    source_account_identity = source_policy.live_account_identity_digest
    if (
        set(marker) != _STRESS90_MARKER_FIELDS
        or marker.get("policy_id") != STRESS90_POLICY.policy_id
        or marker.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
        or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
        or marker.get("bootstrap_seed_digest") != source_policy.bootstrap_seed_digest
        or source_account_identity is None
        or marker.get("account_identity_digest") != source_account_identity
    ):
        raise Stress90LifecycleTransactionError(
            "lifecycle Stress-90 source cross-file identity mismatch"
        )
    if operation in {
        "activation",
        "settlement_roll_forward",
        "stress90_to_execution_aligned",
    } and (source_account_identity != account_identity_digest):
        raise Stress90LifecycleTransactionError("lifecycle source account identity mismatch")
    if operation == "activation":
        raise Stress90LifecycleTransactionError(
            "Stress-90 is already activated; activation cannot reset the account path or "
            "daily-loss baseline—use only the exact committed operation-id or explicit rebase"
        )
    return source_account_identity


def _pristine_unbound_account_path(state: Stress90PolicyState) -> bool:
    return bool(
        state.completed_account_wealth == 1.0
        and state.completed_account_high_watermark == 1.0
        and state.last_completed_account_day is None
        and not state.recent_daily_returns_for_adaptive_margin
        and state.live_inception_day is None
        and state.live_inception_equity is None
        and state.live_account_identity_digest is None
        and state.live_account_epoch is None
    )


def _require_only_allowed_delta(
    source: object,
    target: object,
    *,
    allowed_fields: set[str],
    label: str,
) -> None:
    source_values = vars(source)
    target_values = vars(target)
    changed = sorted(
        name
        for name in source_values
        if source_values[name] != target_values.get(name) and name not in allowed_fields
    )
    if changed:
        raise Stress90LifecycleTransactionError(
            f"{label} transition changed forbidden fields: {', '.join(changed)}"
        )


def _require_other_strategy_states_unchanged(
    source: RuntimeState,
    target: RuntimeState,
    *,
    label: str,
) -> None:
    source_other = {
        key: value
        for key, value in source.strategy_states.items()
        if key != _POLICY_IDENTITY_STATE_KEY
    }
    target_other = {
        key: value
        for key, value in target.strategy_states.items()
        if key != _POLICY_IDENTITY_STATE_KEY
    }
    if source_other != target_other:
        raise Stress90LifecycleTransactionError(
            f"{label} transition changed unrelated strategy state"
        )


def _validate_operation_transition(
    *,
    operation: str,
    generic_source: RuntimeStateRecord | None,
    policy_source: Stress90StateRecord,
    generic_target: RuntimeState,
    policy_target: Stress90PolicyState,
    trading_day: str,
    account_snapshot: AccountSnapshot,
    source_account_identity_digest: str,
    account_identity_digest: str,
    account_transition: Stress90LifecycleAccountTransition | None,
) -> None:
    source_policy = policy_source.state
    if operation == "settlement_roll_forward":
        if generic_source is None or account_transition is None:
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward source/account transition is missing"
            )
        source_generic = generic_source.state
        _require_only_allowed_delta(
            source_policy,
            policy_target,
            allowed_fields=_SETTLEMENT_POLICY_MUTABLE_FIELDS,
            label="settlement roll-forward policy target",
        )
        _require_only_allowed_delta(
            source_generic,
            generic_target,
            allowed_fields=_SETTLEMENT_GENERIC_MUTABLE_FIELDS,
            label="settlement roll-forward generic target",
        )
        _require_other_strategy_states_unchanged(
            source_generic,
            generic_target,
            label="settlement roll-forward",
        )
        expected = build_stress90_settlement_roll_forward_targets(
            source_generic,
            source_policy,
            account_snapshot,
        )
        expected_generic = expected.generic_target
        if policy_target != expected.policy_target:
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward policy target does not match completed return"
            )
        if (
            generic_target.positions != source_generic.positions
            or generic_target.trading_day != trading_day
            or generic_target.last_account_trading_day != trading_day
            or generic_target.day_start_equity != expected_generic.day_start_equity
            or generic_target.equity_high_watermark != expected_generic.equity_high_watermark
            or generic_target.last_account_equity != expected_generic.last_account_equity
            or generic_target.last_account_deposit != expected_generic.last_account_deposit
            or generic_target.last_account_withdrawal != expected_generic.last_account_withdrawal
            or generic_target.last_account_cash_flow_verified
            is not expected_generic.last_account_cash_flow_verified
            or generic_target.last_account_settlement_id
            != expected_generic.last_account_settlement_id
            or generic_target.recent_daily_returns != expected_generic.recent_daily_returns
        ):
            raise Stress90LifecycleTransactionError(
                "settlement roll-forward generic target does not match account transition"
            )
        return

    if operation == "activation":
        if account_transition is None:
            raise Stress90LifecycleTransactionError(
                "activation account transition evidence is missing"
            )
        _require_only_allowed_delta(
            source_policy,
            policy_target,
            allowed_fields=_ACTIVATION_POLICY_MUTABLE_FIELDS,
            label="activation policy target",
        )
        if policy_target.live_inception_day != trading_day:
            raise Stress90LifecycleTransactionError(
                "activation policy initialization day is invalid"
            )
        if policy_target.live_inception_equity != float(account_snapshot.equity):
            raise Stress90LifecycleTransactionError(
                "activation policy inception equity does not match account evidence"
            )
        if (
            source_policy.live_inception_day is not None
            and policy_target.live_inception_day != source_policy.live_inception_day
        ):
            raise Stress90LifecycleTransactionError(
                "activation cannot rewrite an existing account path inception"
            )
        if generic_source is not None:
            _require_only_allowed_delta(
                generic_source.state,
                generic_target,
                allowed_fields=_ACTIVATION_GENERIC_MUTABLE_FIELDS,
                label="activation generic target",
            )
            _require_other_strategy_states_unchanged(
                generic_source.state,
                generic_target,
                label="activation",
            )
        else:
            _require_only_allowed_delta(
                RuntimeState(),
                generic_target,
                allowed_fields=_FRESH_ACTIVATION_GENERIC_MUTABLE_FIELDS,
                label="fresh activation generic target",
            )
            if set(generic_target.strategy_states) != {_POLICY_IDENTITY_STATE_KEY}:
                raise Stress90LifecycleTransactionError(
                    "fresh activation cannot initialize unrelated strategy state"
                )
        if generic_target.day_start_equity != account_transition.day_start_equity:
            raise Stress90LifecycleTransactionError(
                "activation cannot reset or fabricate the hard daily-loss baseline"
            )
        if generic_target.equity_high_watermark != account_transition.equity_high_watermark:
            raise Stress90LifecycleTransactionError(
                "activation cannot reset or fabricate the hard drawdown baseline"
            )
        return

    if generic_source is None:  # source validator provides the operator-facing error
        raise Stress90LifecycleTransactionError(
            f"{operation} requires an existing generic runtime source"
        )
    if operation == "reactivation":
        if account_transition is None:
            raise Stress90LifecycleTransactionError(
                "reactivation account transition evidence is missing"
            )
        _require_migration_account_continuity(generic_source.state, account_snapshot)
        _require_only_allowed_delta(
            source_policy,
            policy_target,
            allowed_fields=_REBASE_POLICY_MUTABLE_FIELDS,
            label="reactivation policy target",
        )
        _require_only_allowed_delta(
            generic_source.state,
            generic_target,
            allowed_fields=_REACTIVATION_GENERIC_MUTABLE_FIELDS,
            label="reactivation generic target",
        )
        _require_other_strategy_states_unchanged(
            generic_source.state,
            generic_target,
            label="reactivation",
        )
        if (
            account_transition.verified_deposit_delta != 0.0
            or account_transition.verified_withdrawal_delta != 0.0
            or generic_target.day_start_equity != generic_source.state.day_start_equity
            or generic_target.equity_high_watermark != generic_source.state.equity_high_watermark
        ):
            raise Stress90LifecycleTransactionError(
                "reactivation cannot change cash flow or hard risk baselines"
            )
        if (
            policy_target.completed_account_wealth != 1.0
            or policy_target.completed_account_high_watermark != 1.0
            or policy_target.last_completed_account_day is not None
            or policy_target.recent_daily_returns_for_adaptive_margin
            or policy_target.live_inception_day != trading_day
            or policy_target.live_inception_equity != float(account_snapshot.equity)
            or policy_target.live_account_identity_digest != account_identity_digest
        ):
            raise Stress90LifecycleTransactionError(
                "reactivation must explicitly reset only the Stress-90 soft account path"
            )
        if generic_target.recent_daily_returns:
            raise Stress90LifecycleTransactionError(
                "reactivation generic and policy return windows must both be empty"
            )
        return
    if operation == "account_rebase":
        if account_transition is None:
            raise Stress90LifecycleTransactionError("account rebase transition evidence is missing")
        _require_only_allowed_delta(
            source_policy,
            policy_target,
            allowed_fields=_REBASE_POLICY_MUTABLE_FIELDS,
            label="account rebase policy target",
        )
        _require_only_allowed_delta(
            generic_source.state,
            generic_target,
            allowed_fields=_REBASE_GENERIC_MUTABLE_FIELDS,
            label="account rebase generic target",
        )
        _require_other_strategy_states_unchanged(
            generic_source.state,
            generic_target,
            label="account rebase",
        )
        if generic_target.recent_daily_returns:
            raise Stress90LifecycleTransactionError(
                "account rebase must clear generic adaptive-margin returns"
            )
        if policy_target.live_inception_equity != float(account_snapshot.equity):
            raise Stress90LifecycleTransactionError(
                "account rebase policy inception equity does not match account evidence"
            )
        source_marker = generic_source.state.strategy_states.get(_POLICY_IDENTITY_STATE_KEY)
        target_marker = generic_target.strategy_states.get(_POLICY_IDENTITY_STATE_KEY)
        if not isinstance(source_marker, dict) or not isinstance(target_marker, dict):
            raise Stress90LifecycleTransactionError(
                "account rebase policy binding transition is invalid"
            )
        source_binding = {
            key: value for key, value in source_marker.items() if key != "account_identity_digest"
        }
        target_binding = {
            key: value for key, value in target_marker.items() if key != "account_identity_digest"
        }
        if source_binding != target_binding:
            raise Stress90LifecycleTransactionError(
                "account rebase may only change the policy account binding"
            )
        switched_account = source_account_identity_digest != account_identity_digest
        source_generic = generic_source.state
        deposit_delta = account_transition.verified_deposit_delta
        withdrawal_delta = account_transition.verified_withdrawal_delta
        if not switched_account and deposit_delta == 0.0 and withdrawal_delta == 0.0:
            raise Stress90LifecycleTransactionError(
                "same-account rebase requires a verified deposit or withdrawal change"
            )
        if switched_account:
            if (
                generic_target.last_order_id
                or generic_target.last_trade_id
                or generic_target.recent_trade_ids
            ):
                raise Stress90LifecycleTransactionError(
                    "account switch rebase must clear old account trade identities"
                )
        else:
            if (
                generic_target.last_order_id != source_generic.last_order_id
                or generic_target.last_trade_id != source_generic.last_trade_id
                or generic_target.recent_trade_ids != source_generic.recent_trade_ids
            ):
                raise Stress90LifecycleTransactionError(
                    "same-account rebase cannot clear order/trade ownership evidence"
                )
        if generic_target.equity_high_watermark != account_transition.equity_high_watermark:
            raise Stress90LifecycleTransactionError(
                "generic account hard drawdown baseline transition is invalid"
            )
        if generic_target.day_start_equity != account_transition.day_start_equity:
            raise Stress90LifecycleTransactionError(
                "generic account hard daily-loss baseline transition is invalid"
            )
        return

    _require_migration_account_continuity(generic_source.state, account_snapshot)
    if policy_target != source_policy:
        raise Stress90LifecycleTransactionError(
            "migration policy target must remain completely unchanged"
        )
    _require_only_allowed_delta(
        generic_source.state,
        generic_target,
        allowed_fields=_MIGRATION_GENERIC_MUTABLE_FIELDS,
        label="migration generic target",
    )
    _require_other_strategy_states_unchanged(
        generic_source.state,
        generic_target,
        label="migration",
    )


class Stress90LifecycleTransactionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve(strict=False)

    @property
    def lineage_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lineage")

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(self.path.name + ".prev")

    def _load_record(self) -> _TransactionRecord | None:
        current_exists = self.path.exists() or self.path.is_symlink()
        previous_exists = self.previous_path.exists() or self.previous_path.is_symlink()
        lineage_exists = self.lineage_path.exists() or self.lineage_path.is_symlink()
        if not current_exists:
            if previous_exists:
                raise Stress90LifecycleTransactionError(
                    "current lifecycle transaction is missing while .prev evidence exists"
                )
            if lineage_exists:
                raise Stress90LifecycleTransactionError(
                    "lifecycle transaction lineage exists while current is missing"
                )
            return None
        if not lineage_exists:
            raise Stress90LifecycleTransactionError(
                "lifecycle transaction lineage marker is missing"
            )
        if self._read_lineage_marker() != _LINEAGE_MARKER:
            raise Stress90LifecycleTransactionError(
                "lifecycle transaction lineage marker is invalid"
            )
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Stress90LifecycleTransactionError(
                "invalid Stress-90 lifecycle transaction JSON"
            ) from exc
        fields = {"kind", "schema_version", "sequence", "transaction", "checksum"}
        if not isinstance(raw, Mapping) or set(raw) != fields:
            raise Stress90LifecycleTransactionError(
                "Stress-90 lifecycle transaction envelope is invalid"
            )
        if raw["kind"] != _KIND or raw["schema_version"] != _SCHEMA:
            raise Stress90LifecycleTransactionError(
                "Stress-90 lifecycle transaction schema is invalid"
            )
        sequence = _positive_int(raw["sequence"], "lifecycle transaction sequence")
        checksum = _sha(raw["checksum"], "lifecycle transaction checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != _digest(unsigned):
            raise Stress90LifecycleTransactionError(
                "Stress-90 lifecycle transaction checksum mismatch"
            )
        return _TransactionRecord(
            transaction=_decode_transaction(raw["transaction"]),
            sequence=sequence,
            checksum=checksum,
        )

    def load(self) -> Stress90LifecycleTransaction | None:
        record = self._load_record()
        return None if record is None else record.transaction

    def load_required(self) -> Stress90LifecycleTransaction:
        transaction = self.load()
        if transaction is None:
            raise Stress90LifecycleTransactionError(
                "required Stress-90 lifecycle transaction is missing"
            )
        return transaction

    def begin(
        self,
        *,
        operation: str,
        generic_source: RuntimeStateRecord | None,
        policy_source: Stress90StateRecord,
        generic_target: RuntimeState,
        policy_target: Stress90PolicyState,
        trading_day: str,
        account_identity_digest: str,
        account_snapshot: AccountSnapshot,
        operation_nonce: str,
        operator_reason: str | None = None,
        account_day_continuity_digest: str = "",
    ) -> Stress90LifecycleTransaction:
        current = self._load_record()
        if current is not None and current.transaction.status == "prepared":
            raise Stress90LifecycleTransactionError(
                "a Stress-90 lifecycle transaction is already pending"
            )
        if operation not in _OPERATIONS:
            raise Stress90LifecycleTransactionError("unsupported lifecycle operation")
        source_sequence = 0 if generic_source is None else generic_source.sequence
        source_checksum = "" if generic_source is None else generic_source.checksum
        normalized_day = _day(trading_day)
        normalized_account_identity = _sha(account_identity_digest, "account identity")
        normalized_operation_nonce = _sha(operation_nonce, "lifecycle operation nonce")
        target_marker = generic_target.strategy_states.get(_POLICY_IDENTITY_STATE_KEY)
        normalized_reason = _reason(
            operator_reason
            if operator_reason is not None
            else (target_marker.get("operator_reason") if isinstance(target_marker, dict) else None)
        )
        if not isinstance(account_snapshot, AccountSnapshot):
            raise Stress90LifecycleTransactionError(
                "lifecycle begin requires an exact verified account snapshot"
            )
        if account_snapshot.trading_day != normalized_day:
            raise Stress90LifecycleTransactionError(
                "lifecycle account snapshot/trading day mismatch"
            )
        account_evidence_scope = (
            "settlement_snapshot" if operation == "settlement_roll_forward" else "account_snapshot"
        )
        account_evidence = _account_snapshot_evidence_payload(
            account_snapshot,
            account_identity_digest=normalized_account_identity,
        )
        account_evidence_digest = _digest(
            _scoped_account_evidence_payload(
                account_evidence,
                scope=account_evidence_scope,
            )
        )
        same_account_cross_day_rebase = bool(
            operation == "account_rebase"
            and generic_source is not None
            and generic_source.state.trading_day != normalized_day
            and policy_source.state.live_account_identity_digest == normalized_account_identity
        )
        requires_continuity = bool(
            operation == "settlement_roll_forward" or same_account_cross_day_rebase
        )
        normalized_continuity_digest = _sha(
            account_day_continuity_digest,
            "account-day continuity digest",
            allow_empty=not requires_continuity,
        )
        if (
            operation not in {"account_rebase", "settlement_roll_forward"}
            and normalized_continuity_digest
        ):
            raise Stress90LifecycleTransactionError(
                "account-day continuity evidence is only valid for settlement roll-forward"
            )
        if operation == "account_rebase" and bool(normalized_continuity_digest) != (
            same_account_cross_day_rebase
        ):
            raise Stress90LifecycleTransactionError(
                "account rebase continuity evidence does not match its account-day transition"
            )
        normalized_continuity_source_day = (
            _day(generic_source.state.trading_day)
            if requires_continuity and generic_source is not None
            else ""
        )
        if normalized_continuity_source_day and (
            normalized_continuity_source_day >= normalized_day
        ):
            raise Stress90LifecycleTransactionError(
                "account-day continuity source must precede its target day"
            )
        source_account_identity = _validate_begin_source_invariants(
            operation=operation,
            generic_source=generic_source,
            policy_source=policy_source,
            policy_target=policy_target,
            account_identity_digest=normalized_account_identity,
            trading_day=normalized_day,
        )
        target_marker = generic_target.strategy_states.get(_POLICY_IDENTITY_STATE_KEY)
        if policy_target.live_account_identity_digest != normalized_account_identity:
            raise Stress90LifecycleTransactionError("lifecycle target account identity mismatch")
        if (
            not isinstance(target_marker, dict)
            or target_marker.get("account_identity_digest") != normalized_account_identity
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle generic target account identity mismatch"
            )
        account_transition = None
        if operation in {
            "activation",
            "reactivation",
            "account_rebase",
            "settlement_roll_forward",
        }:
            account_transition = stress90_lifecycle_account_transition(
                operation=operation,
                generic_source=(None if generic_source is None else generic_source.state),
                account_snapshot=account_snapshot,
                account_switched=(source_account_identity != normalized_account_identity),
            )
        _validate_operation_transition(
            operation=operation,
            generic_source=generic_source,
            policy_source=policy_source,
            generic_target=generic_target,
            policy_target=policy_target,
            trading_day=normalized_day,
            account_snapshot=account_snapshot,
            source_account_identity_digest=source_account_identity,
            account_identity_digest=normalized_account_identity,
            account_transition=account_transition,
        )
        verified_deposit_delta = 0.0
        verified_withdrawal_delta = 0.0
        if operation == "account_rebase" and account_transition is not None:
            verified_deposit_delta = account_transition.verified_deposit_delta
            verified_withdrawal_delta = account_transition.verified_withdrawal_delta
        provisional = Stress90LifecycleTransaction(
            transaction_id="0" * 64,
            operation_nonce=normalized_operation_nonce,
            operation=operation,
            operator_reason=normalized_reason,
            status="prepared",
            trading_day=normalized_day,
            source_account_identity_digest=source_account_identity,
            source_account_epoch=(policy_source.state.live_account_epoch or ""),
            account_identity_digest=normalized_account_identity,
            account_evidence_scope=account_evidence_scope,
            account_evidence=MappingProxyType(account_evidence),
            account_evidence_digest=account_evidence_digest,
            account_day_continuity_digest=normalized_continuity_digest,
            account_day_continuity_source_day=normalized_continuity_source_day,
            verified_deposit_delta=verified_deposit_delta,
            verified_withdrawal_delta=verified_withdrawal_delta,
            generic_source_sequence=source_sequence,
            generic_source_checksum=source_checksum,
            policy_source_sequence=policy_source.sequence,
            policy_source_checksum=policy_source.checksum,
            generic_target=generic_target,
            policy_target=policy_target,
        )
        # Decode our own payload before any write so both state validators and the
        # HALTED invariant are applied identically on creation and restart.
        identity_payload = _transaction_payload(provisional)
        identity_payload.pop("transaction_id")
        transaction = replace(provisional, transaction_id=_digest(identity_payload))
        _decode_transaction(_transaction_payload(transaction))
        if (
            current is not None
            and current.transaction.status == "committed"
            and current.transaction.transaction_id == transaction.transaction_id
        ):
            return current.transaction
        if (
            current is not None
            and current.transaction.status == "committed"
            and current.transaction.operation_nonce == normalized_operation_nonce
        ):
            raise Stress90LifecycleTransactionError(
                "lifecycle operation nonce was already committed for a different request"
            )
        self._save_record(current, transaction)
        return transaction

    def mark_committed(self, transaction_id: str) -> Stress90LifecycleTransaction:
        current = self._load_record()
        if current is None or current.transaction.transaction_id != transaction_id:
            raise Stress90LifecycleTransactionError("lifecycle transaction changed before commit")
        if current.transaction.status == "committed":
            return current.transaction
        committed = replace(current.transaction, status="committed")
        self._save_record(current, committed)
        return committed

    def _save_record(
        self,
        current: _TransactionRecord | None,
        transaction: Stress90LifecycleTransaction,
    ) -> None:
        sequence = 1 if current is None else current.sequence + 1
        unsigned = {
            "kind": _KIND,
            "schema_version": _SCHEMA,
            "sequence": sequence,
            "transaction": _transaction_payload(transaction),
        }
        encoded = json.dumps(
            {**unsigned, "checksum": _digest(unsigned)},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous_bytes = None if current is None else self.path.read_bytes()
        if current is None:
            self._create_lineage_marker()
        # The authoritative current replace is the single commit point. `.prev` is
        # evidence only and is never required or selected as automatic recovery input.
        self._atomic_replace(self.path, encoded)
        if previous_bytes is not None:
            self._atomic_replace(self.previous_path, previous_bytes)

    def _create_lineage_marker(self) -> None:
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
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise Stress90LifecycleTransactionError(
                "lifecycle transaction lineage marker creation failed"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _read_lineage_marker(self) -> bytes:
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.lineage_path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                return handle.read()
        except OSError as exc:
            raise Stress90LifecycleTransactionError(
                "lifecycle transaction lineage marker cannot be read"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _atomic_replace(path: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            with NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def _generic_is_source(
    record: RuntimeStateRecord | None,
    transaction: Stress90LifecycleTransaction,
) -> bool:
    if transaction.generic_source_sequence == 0:
        return record is None
    return bool(
        record is not None
        and record.sequence == transaction.generic_source_sequence
        and record.checksum == transaction.generic_source_checksum
    )


def _generic_is_target(
    record: RuntimeStateRecord | None,
    transaction: Stress90LifecycleTransaction,
) -> bool:
    return bool(
        record is not None
        and record.sequence == transaction.generic_source_sequence + 1
        and record.state == transaction.generic_target
    )


def _policy_is_source(
    record: Stress90StateRecord,
    transaction: Stress90LifecycleTransaction,
) -> bool:
    return bool(
        record.sequence == transaction.policy_source_sequence
        and record.checksum == transaction.policy_source_checksum
    )


def _policy_is_target(
    record: Stress90StateRecord,
    transaction: Stress90LifecycleTransaction,
) -> bool:
    return bool(
        record.sequence == transaction.policy_source_sequence + 1
        and record.state == transaction.policy_target
    )


def apply_stress90_lifecycle_transaction(
    transaction_store: Stress90LifecycleTransactionStore,
    *,
    generic_store: StateStore,
    policy_store: Stress90PolicyStateStore,
    precommit_check: Callable[[], None] | None = None,
) -> Stress90LifecycleTransaction:
    """Idempotently roll one prepared lifecycle operation forward, never backward."""

    transaction = transaction_store.load_required()
    if transaction.status == "committed":
        policy = policy_store.load_required_record()
        generic = generic_store.load_record()
        if not _policy_is_target(policy, transaction) or not _generic_is_target(
            generic, transaction
        ):
            raise Stress90LifecycleTransactionError(
                "committed lifecycle target states no longer match exact transaction"
            )
        return transaction

    policy = policy_store.load_required_record()
    if _policy_is_source(policy, transaction):
        policy_store.save(
            transaction.policy_target,
            expected_sequence=transaction.policy_source_sequence,
        )
    elif not _policy_is_target(policy, transaction):
        raise Stress90LifecycleTransactionError(
            "policy state has an unrelated revision during lifecycle recovery"
        )

    generic = generic_store.load_record()
    if _generic_is_source(generic, transaction):
        generic_store.save(
            transaction.generic_target,
            expected_sequence=transaction.generic_source_sequence,
            expected_checksum=transaction.generic_source_checksum,
        )
    elif not _generic_is_target(generic, transaction):
        raise Stress90LifecycleTransactionError(
            "generic state has an unrelated revision during lifecycle recovery"
        )

    # Re-read both authoritative current files before committing the coordinator.
    final_policy = policy_store.load_required_record()
    final_generic = generic_store.load_record()
    if not _policy_is_target(final_policy, transaction) or not _generic_is_target(
        final_generic, transaction
    ):
        raise Stress90LifecycleTransactionError(
            "lifecycle target states are incomplete after roll-forward"
        )
    if precommit_check is not None:
        precommit_check()
    return transaction_store.mark_committed(transaction.transaction_id)


def require_matching_stress90_lifecycle_account_evidence(
    transaction: Stress90LifecycleTransaction,
    account: AccountSnapshot,
    *,
    account_identity_digest: str,
) -> None:
    """Require a resumed operation to observe the exact prepared Broker snapshot."""

    if account_identity_digest != transaction.account_identity_digest:
        raise Stress90LifecycleTransactionError("Stress-90 lifecycle account identity mismatch")
    actual = stress90_lifecycle_account_evidence_digest(
        account,
        account_identity_digest=account_identity_digest,
        scope=transaction.account_evidence_scope,
    )
    if actual != transaction.account_evidence_digest:
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle account snapshot/evidence changed or mismatched"
        )


def require_no_pending_stress90_lifecycle_transaction(runtime_dir: str | Path) -> None:
    store = Stress90LifecycleTransactionStore(
        Path(runtime_dir) / "stress90_lifecycle_transaction.json"
    )
    transaction = store.load()
    if transaction is not None and transaction.status == "prepared":
        raise Stress90LifecycleTransactionError(
            "Stress-90 lifecycle transaction is pending; resume the exact lifecycle CLI"
        )
