"""One-shot technical Doctor authority for a Stress-90 HALTED runtime."""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import AccountSnapshot, ContractInfo, ContractPosition, RuntimeMode, Trade

STRESS90_ACTIVATION_PERMIT_KIND = "afuture.directional.stress90.activation-permit"
STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION = 6
STRESS90_ACTIVATION_PERMIT_ACK = "I_CONFIRM_STRESS90_TECHNICAL_ACTIVATION"
STRESS90_ACTIVATION_PERMIT_SCOPE = "technical-runtime-activation-only"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PERMIT_STATUSES = frozenset({"issued", "consumed", "invalidated"})
STRESS90_DOCTOR_INTERNAL_P0_CHECKS = frozenset(
    {
        "state_integrity",
        "runtime_paths_writable",
        "disk_space",
        "broker_ready",
        "fresh_snapshot",
        "account_snapshot_valid",
        "trading_day_consistent",
        "no_active_orders",
        "contract_catalog_available",
        "live_metadata_complete",
        "configured_metadata_conservative",
        "position_reconciliation",
        "account_risk_limits",
        "directional_activity_ready",
        "directional_ohlc_cache_integrity",
        "directional_ohlc_cache_required_day",
        "stress90_seed_integrity",
        "stress90_policy_state_integrity",
        "stress90_seed_state_identity",
        "stress90_runtime_policy_identity",
        "stress90_risk_overlay_identity",
        "stress90_oi_evidence_integrity",
        "stress90_execution_intent_integrity",
        "stress90_ctp_order_journal_integrity",
        "stress90_lifecycle_transaction",
        "stress90_account_path_continuity",
        "stress90_account_runtime_registry",
        "stress90_session_trade_ownership",
        "stress90_live_quote_coverage",
        "stress90_live_cost_compatibility",
        "stress90_target_day_continuity",
        "stress90_integer_preview",
        "stress90_risk_manager_preview",
        "stress90_first_activation_gates",
    }
)


class Stress90ActivationPermitIntegrityError(RuntimeError):
    """Technical activation evidence or its one-shot transition is untrusted."""


@dataclass(frozen=True)
class Stress90ActivationEvidence:
    account_identity_digest: str
    account_snapshot_digest: str
    policy_account_epoch: str
    account_registry_sequence: int
    account_registry_checksum: str
    account_registry_runtime_identity_digest: str
    ctp_trading_day: str
    policy_id: str
    policy_definition_digest: str
    policy_manifest_digest: str
    products_manifest_digest: str
    bootstrap_seed_digest: str
    generic_state_sequence: int
    generic_state_checksum: str
    policy_state_sequence: int
    policy_state_checksum: str
    policy_last_decision_digest: str
    policy_last_completed_target_day: str
    ohlc_content_digest: str
    oi_state_sequence: int
    oi_state_checksum: str
    oi_latest_completed_digest: str
    activity_digest: str
    catalog_digest: str
    local_positions_digest: str
    broker_positions_digest: str
    reconciliation_digest: str
    session_trades_digest: str
    session_activity_source_account_identity_digest: str
    session_activity_orders_digest: str
    session_activity_trades_digest: str
    session_activity_ownership_digest: str
    active_order_count: int
    risk_overlay_digest: str
    account_continuity_mode: str = "strict"
    operator_continuity_receipt_digest: str = ""


@dataclass(frozen=True)
class Stress90ActivationPermit:
    permit_id: str
    status: str
    evidence: Stress90ActivationEvidence
    evidence_digest: str
    scope: str = STRESS90_ACTIVATION_PERMIT_SCOPE
    external_activation_gates_completed: bool = False
    invalidation_reason: str = ""


@dataclass(frozen=True)
class Stress90ActivationPermitRecord:
    permit: Stress90ActivationPermit
    sequence: int
    parent_checksum: str | None
    checksum: str


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
        raise Stress90ActivationPermitIntegrityError(
            "Stress-90 activation permit is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _valid_sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise Stress90ActivationPermitIntegrityError(f"{name} must be a lowercase SHA-256")
    return value


def _valid_day(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise Stress90ActivationPermitIntegrityError(f"{name} must be YYYYMMDD")
    try:
        parsed = datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise Stress90ActivationPermitIntegrityError(f"{name} must be YYYYMMDD") from exc
    if parsed != value:
        raise Stress90ActivationPermitIntegrityError(f"{name} must be YYYYMMDD")
    return value


def _valid_positive_integer(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Stress90ActivationPermitIntegrityError(f"{name} must be a positive integer")
    return value


def _evidence_payload(evidence: Stress90ActivationEvidence) -> dict[str, object]:
    if not isinstance(evidence, Stress90ActivationEvidence):
        raise Stress90ActivationPermitIntegrityError(
            "Stress-90 activation evidence type is invalid"
        )
    if not isinstance(evidence.policy_id, str) or not evidence.policy_id.strip():
        raise Stress90ActivationPermitIntegrityError("Stress-90 activation policy id is invalid")
    _valid_sha256(evidence.account_identity_digest, name="account identity digest")
    _valid_sha256(evidence.account_snapshot_digest, name="account snapshot digest")
    _valid_sha256(evidence.policy_account_epoch, name="policy account epoch")
    _valid_day(evidence.ctp_trading_day, name="CTP trading day")
    _valid_day(
        evidence.policy_last_completed_target_day,
        name="policy last completed target day",
    )
    _valid_positive_integer(evidence.generic_state_sequence, name="generic state sequence")
    _valid_positive_integer(evidence.policy_state_sequence, name="policy state sequence")
    _valid_positive_integer(evidence.oi_state_sequence, name="OI state sequence")
    _valid_positive_integer(
        evidence.account_registry_sequence,
        name="account runtime registry sequence",
    )
    for name in (
        "policy_definition_digest",
        "account_registry_checksum",
        "account_registry_runtime_identity_digest",
        "policy_manifest_digest",
        "products_manifest_digest",
        "bootstrap_seed_digest",
        "generic_state_checksum",
        "policy_state_checksum",
        "policy_last_decision_digest",
        "ohlc_content_digest",
        "oi_state_checksum",
        "oi_latest_completed_digest",
        "activity_digest",
        "catalog_digest",
        "local_positions_digest",
        "broker_positions_digest",
        "reconciliation_digest",
        "session_trades_digest",
        "session_activity_source_account_identity_digest",
        "session_activity_orders_digest",
        "session_activity_trades_digest",
        "session_activity_ownership_digest",
    ):
        _valid_sha256(getattr(evidence, name), name=name.replace("_", " "))
    if (
        isinstance(evidence.active_order_count, bool)
        or not isinstance(evidence.active_order_count, int)
        or evidence.active_order_count < 0
    ):
        raise Stress90ActivationPermitIntegrityError(
            "active order count must be a non-negative integer"
        )
    _valid_sha256(evidence.risk_overlay_digest, name="risk overlay digest")
    if evidence.account_continuity_mode not in {"strict", "operator_managed"}:
        raise Stress90ActivationPermitIntegrityError(
            "activation evidence account continuity mode is invalid"
        )
    if evidence.account_continuity_mode == "strict":
        if evidence.operator_continuity_receipt_digest != "":
            raise Stress90ActivationPermitIntegrityError(
                "strict activation evidence cannot bind an operator continuity receipt"
            )
    else:
        _valid_sha256(
            evidence.operator_continuity_receipt_digest,
            name="operator continuity receipt digest",
        )
    return asdict(evidence)


def stress90_activation_evidence_digest(evidence: Stress90ActivationEvidence) -> str:
    return _digest(_evidence_payload(evidence))


def _catalog_digest(catalog: list[ContractInfo]) -> str:
    rows: list[dict[str, str]] = []
    # CTP symbols are the globally unique lookup key used by the runtime catalog.
    seen: set[str] = set()
    for contract in catalog:
        if not isinstance(contract, ContractInfo):
            raise Stress90ActivationPermitIntegrityError("contract catalog row is invalid")
        row = asdict(contract)
        for name in ("symbol", "exchange", "product", "expiry"):
            value = row[name]
            if not isinstance(value, str) or not value or value.strip() != value:
                raise Stress90ActivationPermitIntegrityError(f"contract catalog {name} is invalid")
        listing = row["listing"]
        if not isinstance(listing, str) or listing.strip() != listing:
            raise Stress90ActivationPermitIntegrityError("contract catalog listing is invalid")
        try:
            datetime.fromisoformat(row["expiry"])
            if listing:
                datetime.fromisoformat(listing)
        except ValueError as exc:
            raise Stress90ActivationPermitIntegrityError(
                "contract catalog lifecycle is invalid"
            ) from exc
        if contract.symbol in seen:
            raise Stress90ActivationPermitIntegrityError(
                "contract catalog contains duplicate symbols"
            )
        seen.add(contract.symbol)
        rows.append(row)
    if not rows:
        raise Stress90ActivationPermitIntegrityError("contract catalog is empty")
    return _digest(sorted(rows, key=lambda row: row["symbol"]))


def _positions_payload(positions: list[ContractPosition]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for position in positions:
        if not isinstance(position, ContractPosition):
            raise Stress90ActivationPermitIntegrityError("Broker position row is invalid")
        try:
            position.validate()
        except ValueError as exc:
            raise Stress90ActivationPermitIntegrityError("Broker position row is invalid") from exc
        identity = (position.symbol, position.exchange)
        if identity in seen:
            raise Stress90ActivationPermitIntegrityError(
                "Broker positions contain duplicate identities"
            )
        seen.add(identity)
        rows.append(asdict(position))
    return sorted(rows, key=lambda row: (str(row["symbol"]), str(row["exchange"])))


def _session_trades_digest(
    session_trades: list[Trade],
    *,
    owns_order: Callable[[str], bool],
) -> str:
    if not isinstance(session_trades, list) or not callable(owns_order):
        raise Stress90ActivationPermitIntegrityError(
            "technical activation session trade evidence is invalid"
        )
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for trade in session_trades:
        if not isinstance(trade, Trade):
            raise Stress90ActivationPermitIntegrityError(
                "technical activation session trade row is invalid"
            )
        try:
            trade.validate()
        except ValueError as exc:
            raise Stress90ActivationPermitIntegrityError(
                "technical activation session trade row is invalid"
            ) from exc
        identity = (trade.exchange, trade.trade_id)
        if identity in seen:
            raise Stress90ActivationPermitIntegrityError(
                "technical activation session trades contain duplicate identities"
            )
        seen.add(identity)
        try:
            owned = owns_order(trade.order_id)
        except Exception as exc:
            raise Stress90ActivationPermitIntegrityError(
                "technical activation session trade ownership check failed"
            ) from exc
        if owned is not True:
            raise Stress90ActivationPermitIntegrityError(
                f"unknown session trade {trade.trade_id} for order {trade.order_id}"
            )
        rows.append(
            {
                "trade_id": trade.trade_id,
                "order_id": trade.order_id,
                "symbol": trade.symbol,
                "exchange": trade.exchange,
                "side": trade.side.value,
                "offset": trade.offset.value,
                "volume": trade.volume,
                "price": trade.price,
                "timestamp": trade.timestamp.isoformat(),
                "commission": trade.commission,
            }
        )
    return _digest(
        sorted(
            rows,
            key=lambda row: (
                str(row["exchange"]),
                str(row["trade_id"]),
                str(row["order_id"]),
            ),
        )
    )


def _activity_digest(snapshot) -> str:
    from .directional_activity import validate_directional_activity_snapshot

    validate_directional_activity_snapshot(snapshot)
    return _digest(
        {
            "trading_day": snapshot.trading_day,
            "contracts": {
                symbol: {
                    **asdict(item),
                    "timestamp": item.timestamp.isoformat(),
                }
                for symbol, item in sorted(snapshot.contracts.items())
            },
        }
    )


def collect_stress90_activation_evidence(
    *,
    runtime_dir: str | Path,
    state_store,
    account_identity_digest: str,
    account_snapshot: AccountSnapshot,
    ctp_trading_day: str,
    broker_positions: list[ContractPosition],
    active_orders: list[object],
    session_trades: list[Trade],
    owns_order: Callable[[str], bool],
    session_activity_proof,
    catalog: list[ContractInfo],
    account_registry_path: str | Path | None = None,
    account_continuity_mode: str = "strict",
) -> Stress90ActivationEvidence:
    """Capture the exact trusted local/Broker snapshot a one-shot permit authorizes."""

    from .directional_activity import DirectionalActivityStore
    from .directional_ohlc_cache import DirectionalOHLCCacheStore
    from .directional_policy_activation import require_directional_policy_identity
    from .directional_stress90_oi_runtime import Stress90OiEvidenceStore
    from .directional_stress90_policy import STRESS90_POLICY
    from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
    from .reconcile import compare_positions

    runtime = Path(runtime_dir)
    if state_store.path.parent.resolve() != runtime.resolve():
        raise Stress90ActivationPermitIntegrityError(
            "runtime state and activation permit directory mismatch"
        )
    _valid_sha256(account_identity_digest, name="account identity digest")
    day = _valid_day(ctp_trading_day, name="CTP trading day")
    if not isinstance(account_snapshot, AccountSnapshot):
        raise Stress90ActivationPermitIntegrityError(
            "technical activation account snapshot is invalid"
        )
    if account_snapshot.trading_day != day:
        raise Stress90ActivationPermitIntegrityError(
            "technical activation account/CTP trading day mismatch"
        )
    from .stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        stress90_lifecycle_account_evidence_digest,
        stress90_settlement_amounts_match,
    )

    try:
        account_snapshot_digest = stress90_lifecycle_account_evidence_digest(
            account_snapshot,
            account_identity_digest=account_identity_digest,
        )
    except Stress90LifecycleTransactionError as exc:
        raise Stress90ActivationPermitIntegrityError(
            "technical activation account settlement/cash-flow evidence is unverified"
        ) from exc
    generic = state_store.load_required_record()
    state = generic.state
    if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch:
        raise Stress90ActivationPermitIntegrityError(
            "technical activation evidence requires HALTED state"
        )
    if state.directional_daily_circuit_day:
        raise Stress90ActivationPermitIntegrityError(
            "daily circuit recovery has separate authority"
        )
    if not state.reconciled:
        raise Stress90ActivationPermitIntegrityError(
            "technical activation evidence requires reconciled state"
        )
    source_prebalance = (
        float(state.day_start_equity)
        - float(state.last_account_deposit)
        + float(state.last_account_withdrawal)
    )
    if (
        state.trading_day != day
        or state.last_account_trading_day != day
        or not state.last_account_cash_flow_verified
        or state.last_account_settlement_id != account_snapshot.settlement_id
        or state.last_account_deposit != account_snapshot.deposit
        or state.last_account_withdrawal != account_snapshot.withdrawal
        or not stress90_settlement_amounts_match(
            source_prebalance,
            account_snapshot.previous_settlement_equity,
        )
    ):
        raise Stress90ActivationPermitIntegrityError(
            "technical activation persisted settlement/cash-flow account path is discontinuous"
        )
    if active_orders:
        raise Stress90ActivationPermitIntegrityError(
            "technical activation evidence contains active orders"
        )

    seed = Stress90SeedStore(runtime / "stress90_bootstrap_seed.json").load_required()
    require_directional_policy_identity(
        state,
        policy_id=STRESS90_POLICY.policy_id,
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
        bootstrap_seed_digest=seed.seed_digest,
        account_identity_digest=account_identity_digest,
    )
    policy = Stress90PolicyStateStore(runtime / "stress90_policy_state.json").load_required_record()
    if policy.state.bootstrap_seed_digest != seed.seed_digest:
        raise Stress90ActivationPermitIntegrityError(
            "policy state and bootstrap seed identity mismatch"
        )
    if policy.state.live_account_identity_digest != account_identity_digest:
        raise Stress90ActivationPermitIntegrityError("policy state account identity mismatch")
    if policy.state.live_account_epoch is None:
        raise Stress90ActivationPermitIntegrityError("policy account epoch is missing")
    from .account_runtime_registry import AccountRuntimeRegistry

    registry_path = (
        runtime / ".account-runtime-registry.json"
        if account_registry_path is None
        else Path(account_registry_path)
    )
    registry_evidence = AccountRuntimeRegistry(registry_path).require_binding_evidence(
        account_identity_digest,
        runtime.resolve(strict=False),
        policy.state.live_account_epoch,
    )
    if account_continuity_mode == "strict":
        operator_continuity_receipt_digest = ""
    elif account_continuity_mode == "operator_managed":
        from .stress90_operator_continuity import Stress90OperatorContinuityStore

        operator_receipt = Stress90OperatorContinuityStore(
            runtime / "stress90_operator_continuity.json"
        ).require_current_binding(
            account_identity_digest=account_identity_digest,
            account_epoch=policy.state.live_account_epoch,
            canonical_runtime=runtime,
            target_ctp_trading_day=day,
        )
        from .stress90_operator_continuity import (
            Stress90OperatorContinuityError,
            require_stress90_operator_continuity_authority,
        )
        from .trading_day_evidence import TradingDayEvidenceStore

        try:
            require_stress90_operator_continuity_authority(
                operator_receipt,
                registry_evidence=registry_evidence,
                trading_day_evidence=TradingDayEvidenceStore(
                    runtime / "ctp_trading_day_evidence.json"
                ).load_required(),
            )
        except Stress90OperatorContinuityError as exc:
            raise Stress90ActivationPermitIntegrityError(
                "operator continuity receipt authority is stale or untrusted"
            ) from exc
        operator_continuity_receipt_digest = operator_receipt.checksum
    else:
        raise Stress90ActivationPermitIntegrityError(
            "activation evidence account continuity mode is invalid"
        )
    prepared = policy.state.prepared_decision
    if (
        policy.state.last_completed_target_day != day
        or prepared is None
        or prepared.target_trading_day != day
        or prepared.daily_decision_digest != policy.state.last_decision_digest
    ):
        raise Stress90ActivationPermitIntegrityError(
            "policy/prepared target must be the exact current CTP trading day before permit "
            "issuance or consumption"
        )
    ohlc = DirectionalOHLCCacheStore(runtime / "directional_ohlc_cache.json").load(
        STRESS90_POLICY.products
    )
    if ohlc is None:
        raise Stress90ActivationPermitIntegrityError("directional OHLC cache is missing")
    oi = Stress90OiEvidenceStore(runtime / "stress90_oi_evidence.json").load_required_record()
    if not oi.state.completed or not oi.state.completed[-1].complete:
        raise Stress90ActivationPermitIntegrityError(
            "completed Stress-90 OI evidence is missing or incomplete"
        )
    activity = DirectionalActivityStore(runtime / "directional_activity.json").load()
    if activity is None:
        raise Stress90ActivationPermitIntegrityError(
            "completed directional activity evidence is missing"
        )
    local_positions = state_store.positions_from_state(state)
    local_payload = _positions_payload(local_positions)
    broker_payload = _positions_payload(broker_positions)
    reconciliation = compare_positions(local_positions, broker_positions)
    if not reconciliation.matched:
        raise Stress90ActivationPermitIntegrityError(
            "Broker/local position reconciliation failed: " + reconciliation.details
        )
    local_digest = _digest(local_payload)
    broker_digest = _digest(broker_payload)
    session_digest = _session_trades_digest(session_trades, owns_order=owns_order)
    from .stress90_session_authority import Stress90SessionOwnershipProof

    if not isinstance(session_activity_proof, Stress90SessionOwnershipProof):
        raise Stress90ActivationPermitIntegrityError("complete session activity proof is missing")
    if (
        session_activity_proof.evidence.trading_day != day
        or _session_trades_digest(
            list(session_activity_proof.local_session_trades),
            owns_order=owns_order,
        )
        != session_digest
    ):
        raise Stress90ActivationPermitIntegrityError(
            "complete session activity proof identity mismatch"
        )
    return Stress90ActivationEvidence(
        account_identity_digest=account_identity_digest,
        account_snapshot_digest=account_snapshot_digest,
        policy_account_epoch=policy.state.live_account_epoch,
        account_registry_sequence=registry_evidence.registry_sequence,
        account_registry_checksum=registry_evidence.registry_checksum,
        account_registry_runtime_identity_digest=(
            registry_evidence.binding.runtime_identity_digest
        ),
        ctp_trading_day=day,
        policy_id=STRESS90_POLICY.policy_id,
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        policy_manifest_digest=STRESS90_POLICY.policy_manifest_digest,
        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
        bootstrap_seed_digest=seed.seed_digest,
        risk_overlay_digest=str(
            state.strategy_states["directional_policy_identity"].get("risk_overlay_digest", "")
        ),
        generic_state_sequence=generic.sequence,
        generic_state_checksum=generic.checksum,
        policy_state_sequence=policy.sequence,
        policy_state_checksum=policy.checksum,
        policy_last_decision_digest=policy.state.last_decision_digest,
        policy_last_completed_target_day=policy.state.last_completed_target_day,
        ohlc_content_digest=ohlc.content_digest,
        oi_state_sequence=oi.sequence,
        oi_state_checksum=oi.checksum,
        oi_latest_completed_digest=oi.state.completed[-1].evidence_digest,
        activity_digest=_activity_digest(activity),
        catalog_digest=_catalog_digest(catalog),
        local_positions_digest=local_digest,
        broker_positions_digest=broker_digest,
        reconciliation_digest=_digest(
            {
                "local_positions_digest": local_digest,
                "broker_positions_digest": broker_digest,
                "matched": True,
            }
        ),
        session_trades_digest=session_digest,
        session_activity_source_account_identity_digest=(
            session_activity_proof.evidence.account_identity_digest
        ),
        session_activity_orders_digest=session_activity_proof.evidence.orders_digest,
        session_activity_trades_digest=session_activity_proof.evidence.trades_digest,
        session_activity_ownership_digest=session_activity_proof.ownership_digest,
        active_order_count=0,
        account_continuity_mode=account_continuity_mode,
        operator_continuity_receipt_digest=operator_continuity_receipt_digest,
    )


def activate_stress90_from_permit(
    *,
    state_store,
    permit_store: Stress90ActivationPermitStore,
    evidence: Stress90ActivationEvidence,
    expected_permit_sequence: int,
):
    """Consume first, then perform the sole authoritative HALTED→RUNNING state save."""

    permit_record = permit_store.load_required_record()
    if permit_record.sequence != expected_permit_sequence:
        raise Stress90ActivationPermitIntegrityError(
            "activation permit sequence changed concurrently"
        )
    if permit_record.permit.status != "issued":
        raise Stress90ActivationPermitIntegrityError("activation permit is not issued")
    current = state_store.load_required_record()
    if (
        current.sequence != evidence.generic_state_sequence
        or current.checksum != evidence.generic_state_checksum
    ):
        raise Stress90ActivationPermitIntegrityError(
            "generic runtime state changed after Doctor permit issuance"
        )
    state = current.state
    if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch:
        raise Stress90ActivationPermitIntegrityError(
            "technical activation requires HALTED runtime state"
        )
    if state.directional_daily_circuit_day:
        raise Stress90ActivationPermitIntegrityError(
            "daily circuit recovery has separate authority"
        )
    permit_store.consume(evidence, expected_sequence=permit_record.sequence)
    running = replace(
        state,
        kill_switch=False,
        kill_reason="",
        reconciled=True,
        runtime_mode=RuntimeMode.RUNNING.value,
        reduce_reason="",
    )
    return state_store.save(
        running,
        expected_sequence=current.sequence,
        expected_checksum=current.checksum,
    )


def _permit_payload(permit: Stress90ActivationPermit) -> dict[str, object]:
    if not isinstance(permit, Stress90ActivationPermit):
        raise Stress90ActivationPermitIntegrityError("Stress-90 activation permit type is invalid")
    _valid_sha256(permit.permit_id, name="activation permit id")
    if permit.status not in _PERMIT_STATUSES:
        raise Stress90ActivationPermitIntegrityError("activation permit status is invalid")
    evidence = _evidence_payload(permit.evidence)
    evidence_digest = stress90_activation_evidence_digest(permit.evidence)
    if permit.evidence_digest != evidence_digest:
        raise Stress90ActivationPermitIntegrityError("activation permit evidence digest mismatch")
    if permit.scope != STRESS90_ACTIVATION_PERMIT_SCOPE:
        raise Stress90ActivationPermitIntegrityError("activation permit scope is invalid")
    if permit.external_activation_gates_completed is not False:
        raise Stress90ActivationPermitIntegrityError(
            "technical activation permit cannot complete external activation gates"
        )
    if permit.status == "invalidated":
        if (
            not isinstance(permit.invalidation_reason, str)
            or not permit.invalidation_reason.strip()
        ):
            raise Stress90ActivationPermitIntegrityError(
                "invalidated activation permit requires a reason"
            )
    elif permit.invalidation_reason:
        raise Stress90ActivationPermitIntegrityError(
            "active or consumed activation permit cannot have an invalidation reason"
        )
    return {
        "permit_id": permit.permit_id,
        "status": permit.status,
        "evidence": evidence,
        "evidence_digest": evidence_digest,
        "scope": permit.scope,
        "external_activation_gates_completed": False,
        "invalidation_reason": permit.invalidation_reason,
    }


def _permit_from_payload(raw: object) -> Stress90ActivationPermit:
    required = {
        "permit_id",
        "status",
        "evidence",
        "evidence_digest",
        "scope",
        "external_activation_gates_completed",
        "invalidation_reason",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise Stress90ActivationPermitIntegrityError("activation permit fields are invalid")
    evidence_raw = raw["evidence"]
    expected_evidence_fields = set(Stress90ActivationEvidence.__dataclass_fields__)
    if not isinstance(evidence_raw, dict) or set(evidence_raw) != expected_evidence_fields:
        raise Stress90ActivationPermitIntegrityError("activation evidence fields are invalid")
    try:
        evidence = Stress90ActivationEvidence(**evidence_raw)
        permit = Stress90ActivationPermit(
            permit_id=raw["permit_id"],
            status=raw["status"],
            evidence=evidence,
            evidence_digest=raw["evidence_digest"],
            scope=raw["scope"],
            external_activation_gates_completed=raw["external_activation_gates_completed"],
            invalidation_reason=raw["invalidation_reason"],
        )
    except TypeError as exc:
        raise Stress90ActivationPermitIntegrityError(
            "activation permit values are invalid"
        ) from exc
    _permit_payload(permit)
    return permit


class Stress90ActivationPermitStore:
    """Checksummed one-shot permit state; `.prev` is evidence, never recovery input."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.prev")

    def load_record(self) -> Stress90ActivationPermitRecord | None:
        if not self.path.exists():
            if self.previous_path.exists():
                raise Stress90ActivationPermitIntegrityError(
                    "current activation permit is missing while previous evidence exists"
                )
            return None
        current = self._read_record(self.path)
        self._validate_previous(current)
        return current

    def load_required_record(self) -> Stress90ActivationPermitRecord:
        record = self.load_record()
        if record is None:
            raise Stress90ActivationPermitIntegrityError(
                "required Stress-90 activation permit is missing"
            )
        return record

    def load_previous_record(self) -> Stress90ActivationPermitRecord:
        if not self.previous_path.exists():
            raise Stress90ActivationPermitIntegrityError(
                "previous Stress-90 activation permit is missing"
            )
        return self._read_record(self.previous_path)

    def issue(
        self,
        evidence: Stress90ActivationEvidence,
        *,
        expected_sequence: int | None = None,
    ) -> Stress90ActivationPermitRecord:
        payload = _evidence_payload(evidence)
        if evidence.active_order_count != 0:
            raise Stress90ActivationPermitIntegrityError(
                "technical activation permit requires no active orders"
            )
        if evidence.local_positions_digest != evidence.broker_positions_digest:
            raise Stress90ActivationPermitIntegrityError(
                "technical activation permit requires reconciled positions"
            )
        current = self.load_record()
        current_sequence = 0 if current is None else current.sequence
        if expected_sequence is not None and expected_sequence != current_sequence:
            raise Stress90ActivationPermitIntegrityError(
                "activation permit sequence changed concurrently"
            )
        if current is not None and current.permit.status == "issued":
            raise Stress90ActivationPermitIntegrityError("activation permit is already issued")
        permit = Stress90ActivationPermit(
            permit_id=secrets.token_hex(32),
            status="issued",
            evidence=evidence,
            evidence_digest=_digest(payload),
        )
        return self._save(permit, current)

    def consume(
        self,
        evidence: Stress90ActivationEvidence,
        *,
        expected_sequence: int,
    ) -> Stress90ActivationPermitRecord:
        current = self.load_required_record()
        if current.sequence != expected_sequence:
            raise Stress90ActivationPermitIntegrityError(
                "activation permit sequence changed concurrently"
            )
        if current.permit.status != "issued":
            raise Stress90ActivationPermitIntegrityError("activation permit is not issued")
        actual_digest = stress90_activation_evidence_digest(evidence)
        if actual_digest != current.permit.evidence_digest or evidence != current.permit.evidence:
            raise Stress90ActivationPermitIntegrityError("activation permit evidence mismatch")
        return self._save(replace(current.permit, status="consumed"), current)

    def invalidate(self, reason: str) -> Stress90ActivationPermitRecord | None:
        current = self.load_record()
        if current is None or current.permit.status == "invalidated":
            return current
        if not isinstance(reason, str) or not reason.strip():
            raise Stress90ActivationPermitIntegrityError(
                "activation permit invalidation reason is required"
            )
        return self._save(
            replace(
                current.permit,
                status="invalidated",
                invalidation_reason=reason.strip(),
            ),
            current,
        )

    def _save(
        self,
        permit: Stress90ActivationPermit,
        current: Stress90ActivationPermitRecord | None,
    ) -> Stress90ActivationPermitRecord:
        permit_payload = _permit_payload(permit)
        sequence = 1 if current is None else current.sequence + 1
        parent_checksum = None if current is None else current.checksum
        unsigned: dict[str, object] = {
            "kind": STRESS90_ACTIVATION_PERMIT_KIND,
            "schema_version": STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION,
            "sequence": sequence,
            "parent_checksum": parent_checksum,
            "permit": permit_payload,
        }
        checksum = _digest(unsigned)
        encoded = json.dumps(
            {**unsigned, "checksum": checksum},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if current is not None:
            self._atomic_replace(self.previous_path, self.path.read_bytes())
        self._atomic_replace(self.path, encoded)
        return Stress90ActivationPermitRecord(permit, sequence, parent_checksum, checksum)

    def _read_record(self, path: Path) -> Stress90ActivationPermitRecord:
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Stress90ActivationPermitIntegrityError(
                "invalid Stress-90 activation permit UTF-8"
            ) from exc
        try:
            raw = json.loads(text, object_pairs_hook=self._reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise Stress90ActivationPermitIntegrityError(
                "invalid Stress-90 activation permit JSON"
            ) from exc
        required = {
            "kind",
            "schema_version",
            "sequence",
            "parent_checksum",
            "permit",
            "checksum",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise Stress90ActivationPermitIntegrityError(
                "Stress-90 activation permit envelope fields are invalid"
            )
        if raw["kind"] != STRESS90_ACTIVATION_PERMIT_KIND:
            raise Stress90ActivationPermitIntegrityError("activation permit kind is invalid")
        if raw["schema_version"] != STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION:
            raise Stress90ActivationPermitIntegrityError("activation permit schema is unsupported")
        sequence = _valid_positive_integer(raw["sequence"], name="activation permit sequence")
        parent_checksum = raw["parent_checksum"]
        if sequence == 1:
            if parent_checksum is not None:
                raise Stress90ActivationPermitIntegrityError(
                    "initial activation permit parent checksum is invalid"
                )
        else:
            _valid_sha256(parent_checksum, name="activation permit parent checksum")
        checksum = _valid_sha256(raw["checksum"], name="activation permit checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != _digest(unsigned):
            raise Stress90ActivationPermitIntegrityError("activation permit checksum mismatch")
        return Stress90ActivationPermitRecord(
            permit=_permit_from_payload(raw["permit"]),
            sequence=sequence,
            parent_checksum=parent_checksum,
            checksum=checksum,
        )

    def _validate_previous(self, current: Stress90ActivationPermitRecord) -> None:
        if not self.previous_path.exists():
            if current.sequence != 1:
                raise Stress90ActivationPermitIntegrityError(
                    "previous activation permit evidence is missing"
                )
            return
        previous = self._read_record(self.previous_path)
        if previous.sequence == current.sequence and previous.checksum == current.checksum:
            return
        if previous.sequence + 1 != current.sequence:
            raise Stress90ActivationPermitIntegrityError(
                "previous activation permit sequence is not the predecessor"
            )
        if current.parent_checksum != previous.checksum:
            raise Stress90ActivationPermitIntegrityError(
                "previous activation permit checksum is not the parent"
            )

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise Stress90ActivationPermitIntegrityError(
                    f"duplicate Stress-90 activation permit JSON key: {key}"
                )
            result[key] = value
        return result

    @staticmethod
    def _atomic_replace(target: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            with NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def issue_stress90_doctor_permit(
    *,
    report,
    runtime_dir: str | Path,
    state_store,
    account_identity_digest: str,
    account_snapshot: AccountSnapshot,
    ctp_trading_day: str,
    broker_positions: list[ContractPosition],
    active_orders: list[object],
    session_trades: list[Trade],
    owns_order: Callable[[str], bool],
    session_activity_proof,
    catalog: list[ContractInfo],
    strong_confirmation: str,
    account_registry_path: str | Path | None = None,
    account_continuity_mode: str = "strict",
) -> Stress90ActivationPermitRecord:
    """Issue a technical-only permit from one complete, zero-order Doctor report."""

    if strong_confirmation != STRESS90_ACTIVATION_PERMIT_ACK:
        raise Stress90ActivationPermitIntegrityError(
            "Stress-90 technical activation strong confirmation is missing"
        )
    checks = {
        str(getattr(check, "name", "")): bool(getattr(check, "passed", False))
        for check in getattr(report, "checks", ())
    }
    failures = sorted(
        name for name in STRESS90_DOCTOR_INTERNAL_P0_CHECKS if checks.get(name) is not True
    )
    facts = getattr(report, "facts", None)
    stress = facts.get("stress90") if isinstance(facts, dict) else None
    broker = facts.get("broker") if isinstance(facts, dict) else None
    if failures or not isinstance(stress, dict) or stress.get("stress90_ready") is not True:
        raise Stress90ActivationPermitIntegrityError(
            "Stress-90 Doctor internal P0 checks did not all pass"
        )
    if not isinstance(broker, dict) or broker.get("orders_sent") != 0:
        raise Stress90ActivationPermitIntegrityError(
            "Stress-90 Doctor permit requires orders_sent=0"
        )
    if (
        stress.get("capital_activation_eligible") is not False
        or stress.get("live_eligibility") is not False
        or not isinstance(stress.get("external_blocker_reasons"), list)
        or not stress["external_blocker_reasons"]
    ):
        raise Stress90ActivationPermitIntegrityError(
            "technical permit must retain external activation blockers"
        )
    evidence = collect_stress90_activation_evidence(
        runtime_dir=runtime_dir,
        state_store=state_store,
        account_identity_digest=account_identity_digest,
        account_snapshot=account_snapshot,
        ctp_trading_day=ctp_trading_day,
        broker_positions=broker_positions,
        active_orders=active_orders,
        session_trades=session_trades,
        owns_order=owns_order,
        session_activity_proof=session_activity_proof,
        catalog=catalog,
        account_registry_path=account_registry_path,
        account_continuity_mode=account_continuity_mode,
    )
    store = Stress90ActivationPermitStore(Path(runtime_dir) / "stress90_activation_permit.json")
    current = store.load_record()
    issued = store.issue(
        evidence,
        expected_sequence=0 if current is None else current.sequence,
    )
    stress["technical_activation_permit"] = {
        "status": issued.permit.status,
        "sequence": issued.sequence,
        "permit_id": issued.permit.permit_id,
        "evidence_digest": issued.permit.evidence_digest,
        "scope": issued.permit.scope,
        "external_activation_gates_completed": False,
    }
    report.add(
        "stress90_technical_activation_permit_issued",
        True,
        "one-shot technical permit issued; external activation gates remain blocked",
    )
    return issued


class Stress90TechnicalActivationAuthority:
    """Runtime capability that can consume a permit only under the exact live lease."""

    requires_technical_activation_permit = True

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        account_registry_path: str | Path | None = None,
        account_continuity_mode: str = "strict",
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.account_registry_path = account_registry_path
        if account_continuity_mode not in {"strict", "operator_managed"}:
            raise Stress90ActivationPermitIntegrityError(
                "technical activation account continuity mode is invalid"
            )
        self.account_continuity_mode = account_continuity_mode
        self.permit_store = Stress90ActivationPermitStore(
            self.runtime_dir / "stress90_activation_permit.json"
        )
        from .stress90_session_authority import Stress90StartupSessionAuthority

        self.startup_session_authority = Stress90StartupSessionAuthority(self.runtime_dir)

    def verify_startup_session(self, *, broker, lease, timeout_seconds: float):
        return self.startup_session_authority.verify(
            broker=broker,
            lease=lease,
            timeout_seconds=timeout_seconds,
        )

    def activate(self, *, state_store, broker, lease):
        account_identity = broker.get_account_identity_digest()
        authorizes = getattr(lease, "authorizes_technical_activation", None)
        if not callable(authorizes) or not authorizes(account_identity, self.runtime_dir):
            raise Stress90ActivationPermitIntegrityError(
                "exact account lease is not held for technical activation"
            )
        session_proof = self.startup_session_authority.last_proof
        if session_proof is None:
            raise Stress90ActivationPermitIntegrityError(
                "fresh complete startup session proof is missing"
            )
        require_current = getattr(broker, "require_session_activity_evidence_current", None)
        if not callable(require_current):
            raise Stress90ActivationPermitIntegrityError(
                "Broker cannot revalidate complete startup session proof"
            )
        require_current(session_proof.evidence)
        evidence = collect_stress90_activation_evidence(
            runtime_dir=self.runtime_dir,
            state_store=state_store,
            account_identity_digest=account_identity,
            account_snapshot=broker.get_account(),
            ctp_trading_day=broker.get_trading_day(),
            broker_positions=broker.get_positions(),
            active_orders=broker.get_active_orders(),
            session_trades=broker.get_session_trades(),
            owns_order=broker.owns_order,
            session_activity_proof=session_proof,
            catalog=broker.get_contract_catalog(),
            account_registry_path=self.account_registry_path,
            account_continuity_mode=self.account_continuity_mode,
        )
        permit = self.permit_store.load_required_record()
        return activate_stress90_from_permit(
            state_store=state_store,
            permit_store=self.permit_store,
            evidence=evidence,
            expected_permit_sequence=permit.sequence,
        )
