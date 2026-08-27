"""Read-only operational inspection and fail-closed live preflight checks."""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import isfinite
from pathlib import Path

from .directional_activity import (
    DirectionalActivityStore,
    select_contracts_from_activity,
    validate_directional_activity_snapshot,
)
from .directional_ohlc_cache import (
    OHLC_CACHE_SCHEMA_VERSION,
    DirectionalOHLCCacheIntegrityError,
    DirectionalOHLCCacheStore,
)
from .metadata import validate_contract_metadata
from .models import (
    AccountSnapshot,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    RuntimeMode,
    Tick,
)
from .reconcile import compare_positions
from .state import RuntimeState, StateIntegrityError, StateStore

MIN_OPERATIONAL_DISK_FREE_BYTES = 100 * 1024 * 1024
STRESS90_EXTERNAL_ACTIVATION_GATES = (
    "target_machine_ctp_abi",
    "prior_day_final_funding_settlement_witness",
    "authoritative_nonadjacent_session_ledger",
    "multi_day_shadow",
    "test_counter",
    "observed_live_fees",
    "observed_live_margin",
    "fak_partial_fill",
    "disconnect_reconnect",
    "tiny_live_capital",
    "risk_scale_approval",
)

_STRESS90_EXTERNAL_ACTIVATION_GATE_STATUS = {
    name: "not_verified_by_local_code" for name in STRESS90_EXTERNAL_ACTIVATION_GATES
}
_STRESS90_EXTERNAL_ACTIVATION_GATE_STATUS["authoritative_nonadjacent_session_ledger"] = (
    "not_available_from_pinned_vnpy_ctp_6_7_11_4"
)


def estimate_stress90_contract_cost(
    tick: Tick,
    spec: ContractSpec,
    *,
    hurdle_bps: float = 15.0,
    require_commission_evidence: bool = False,
) -> dict[str, object]:
    """Conservatively compare deterministic live costs with the fixed 15bp evidence."""

    tick.validate()
    if tick.symbol != spec.symbol or tick.exchange.upper() != spec.exchange.upper():
        raise ValueError("cost estimate tick/spec identity mismatch")
    if not isfinite(hurdle_bps) or hurdle_bps <= 0:
        raise ValueError("cost estimate hurdle must be positive")
    mid = float(tick.mid_price)
    notional = mid * float(spec.multiplier)
    if notional <= 0 or spec.price_tick <= 0:
        raise ValueError("cost estimate contract notional/tick is invalid")

    def fee_bps(fixed: float, rate: float) -> float:
        return float((float(fixed) + float(rate) * notional) / notional * 10_000.0)

    fee_values = [float(getattr(spec.fee, name)) for name in spec.fee.__dataclass_fields__]
    if require_commission_evidence and not any(value > 0.0 for value in fee_values):
        raise ValueError("live commission evidence is missing")
    open_fee_per_lot = float(spec.fee.open_fixed) + float(spec.fee.open_rate) * notional
    close_fee_per_lot = float(spec.fee.close_fixed) + float(spec.fee.close_rate) * notional
    close_today_fee_per_lot = (
        float(spec.fee.close_today_fixed) + float(spec.fee.close_today_rate) * notional
    )
    open_fee = fee_bps(spec.fee.open_fixed, spec.fee.open_rate)
    close_fee = fee_bps(spec.fee.close_fixed, spec.fee.close_rate)
    close_today_fee = fee_bps(
        spec.fee.close_today_fixed,
        spec.fee.close_today_rate,
    )
    one_tick = float(spec.price_tick) / mid * 10_000.0
    bid_ask = (float(tick.ask_price) - float(tick.bid_price)) / mid * 10_000.0
    minimum_slippage = max(one_tick, bid_ask / 2.0)
    open_route = open_fee + minimum_slippage
    close_route = close_fee + minimum_slippage
    close_today_route = close_today_fee + minimum_slippage
    minimum_one_way = max(open_route, close_route, close_today_route)
    return {
        "symbol": tick.symbol,
        "exchange": tick.exchange,
        "mid_price": mid,
        "open_fee_per_lot": open_fee_per_lot,
        "close_yesterday_fee_per_lot": close_fee_per_lot,
        "close_today_fee_per_lot": close_today_fee_per_lot,
        "open_fee_bps": open_fee,
        "close_yesterday_fee_bps": close_fee,
        "close_today_fee_bps": close_today_fee,
        "one_tick_bps": one_tick,
        "bid_ask_bps": bid_ask,
        "spread_bps": bid_ask,
        "bid_depth": float(tick.bid_volume),
        "ask_depth": float(tick.ask_volume),
        "minimum_reasonable_slippage_bps": minimum_slippage,
        "open_route_bps": open_route,
        "close_yesterday_route_bps": close_route,
        "close_today_route_bps": close_today_route,
        "minimum_reasonable_one_way_bps": minimum_one_way,
        "historical_hurdle_bps": float(hurdle_bps),
        "historical_15bp_compatible": minimum_one_way <= float(hurdle_bps) + 1e-12,
    }


@dataclass(frozen=True)
class OperationalCheck:
    """One named safety claim with enough detail for operator diagnosis."""

    name: str
    passed: bool
    detail: str


@dataclass
class OperationalReport:
    """JSON-compatible operational facts and checks."""

    checks: list[OperationalCheck] = field(default_factory=list)
    facts: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.checks)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(OperationalCheck(name, passed, detail))

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checks": [asdict(item) for item in self.checks],
            "facts": self.facts,
            "warnings": list(self.warnings),
        }


def _state_facts(state: RuntimeState) -> dict[str, object]:
    return {
        "valid": True,
        "runtime_mode": state.runtime_mode,
        "kill_switch": state.kill_switch,
        "kill_reason": state.kill_reason,
        "reconciled": state.reconciled,
        "metadata_verified": state.metadata_verified,
        "trading_day": state.trading_day,
        "day_start_equity": state.day_start_equity,
        "equity_high_watermark": state.equity_high_watermark,
        "position_count": len(state.positions),
        "last_order_id": state.last_order_id,
        "last_trade_id": state.last_trade_id,
        "recent_trade_id_count": len(state.recent_trade_ids),
        "last_account_equity": state.last_account_equity,
        "last_account_trading_day": state.last_account_trading_day,
    }


def _existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _path_facts(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "symlink": path.is_symlink(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
    }


def _symlink_components(path: Path) -> list[Path]:
    return [candidate for candidate in (path, *path.parents) if candidate.is_symlink()]


def _add_directional_ohlc_cache_status(report: OperationalReport, config) -> None:
    path = Path(config.state_path).with_name("directional_ohlc_cache.json")
    facts: dict[str, object] = {
        "path": str(path),
        "present": path.exists(),
        "valid": None,
    }
    if not config.directional.enabled:
        report.add(
            "directional_ohlc_cache_integrity",
            True,
            "directional strategy is disabled",
        )
        report.facts["directional_ohlc_cache"] = facts
        return
    products = tuple(str(item).upper() for item in config.directional.products)
    try:
        entry = DirectionalOHLCCacheStore(path).load(products)
    except (OSError, DirectionalOHLCCacheIntegrityError) as exc:
        facts.update(valid=False, error=str(exc))
        report.add("directional_ohlc_cache_integrity", False, str(exc))
    else:
        if entry is None:
            report.add(
                "directional_ohlc_cache_integrity",
                True,
                "directional OHLC cache is not initialized",
            )
        else:
            facts.update(
                valid=True,
                schema_version=OHLC_CACHE_SCHEMA_VERSION,
                content_digest=entry.content_digest,
                latest_date=entry.latest_date.isoformat(),
                products=list(entry.products),
                product_count=len(entry.products),
                row_count=entry.row_count,
            )
            report.add(
                "directional_ohlc_cache_integrity",
                True,
                f"verified directional OHLC cache through {entry.latest_date.isoformat()}",
            )
    report.facts["directional_ohlc_cache"] = facts


def _stress90_account_continuity_status(config, runtime_state: RuntimeState | None):
    mode = str(getattr(config.directional, "account_continuity_mode", "strict"))
    runtime_dir = Path(config.state_path).parent.resolve(strict=False)
    operator_path = runtime_dir / "stress90_operator_continuity.json"
    facts: dict[str, object] = {
        "applicable": mode == "operator_managed",
        "path": str(operator_path),
        "present": operator_path.exists(),
        "valid": None,
        "sequence": None,
        "checksum": None,
        "source_ctp_trading_day": None,
        "target_ctp_trading_day": None,
        "account_identity_digest": None,
        "account_epoch": None,
        "canonical_runtime": str(runtime_dir),
        "no_manual_trade": None,
        "no_external_order": None,
        "no_deposit": None,
        "no_withdrawal": None,
        "evidence_authority": "operator_trust" if mode == "operator_managed" else "not_applicable",
        "authoritative_broker_or_exchange_evidence": False,
        "binding_current": False,
    }
    if mode == "strict":
        return (
            facts,
            False,
            False,
            False,
            True,
            "strict continuity mode uses external authoritative gates",
        )
    if mode != "operator_managed":
        return facts, True, False, True, False, "unsupported account continuity mode"

    from .account_runtime_registry import AccountRuntimeRegistry
    from .directional_stress90_state import Stress90PolicyStateStore
    from .stress90_activation_permit import Stress90ActivationPermitStore
    from .stress90_operator_continuity import Stress90OperatorContinuityStore

    store = Stress90OperatorContinuityStore(operator_path)
    try:
        record = store.load_record()
    except (OSError, RuntimeError) as exc:
        facts.update(valid=False, error=str(exc))
        return facts, True, False, True, False, str(exc)
    if record is None:
        return facts, True, False, True, False, "operator continuity receipt is missing"

    evidence = record.evidence
    facts.update(
        present=True,
        valid=True,
        sequence=record.sequence,
        checksum=record.checksum,
        source_ctp_trading_day=evidence.source_ctp_trading_day,
        target_ctp_trading_day=evidence.target_ctp_trading_day,
        natural_day_gap=evidence.natural_day_gap,
        account_identity_digest=evidence.account_identity_digest,
        account_epoch=evidence.account_epoch,
        canonical_runtime=evidence.canonical_runtime,
        no_manual_trade=evidence.no_manual_trade,
        no_external_order=evidence.no_external_order,
        no_deposit=evidence.no_deposit,
        no_withdrawal=evidence.no_withdrawal,
        target_registry_receipt_digest=record.target_registry_receipt_digest,
        target_trading_day_evidence_sequence=record.target_trading_day_evidence_sequence,
        target_trading_day_evidence_checksum=record.target_trading_day_evidence_checksum,
    )
    try:
        policy = Stress90PolicyStateStore(
            runtime_dir / "stress90_policy_state.json"
        ).load_required()
        account = policy.live_account_identity_digest
        epoch = policy.live_account_epoch
        if account is None or epoch is None:
            raise RuntimeError("Stress-90 policy account identity/epoch is missing")
        store.require_current_binding(
            account_identity_digest=account,
            account_epoch=epoch,
            canonical_runtime=runtime_dir,
            target_ctp_trading_day=evidence.target_ctp_trading_day,
        )
        registry_path = Path(
            str(
                getattr(
                    config,
                    "account_registry_path",
                    runtime_dir / ".account-runtime-registry.json",
                )
            )
        )
        registry_evidence = AccountRuntimeRegistry(registry_path).require_binding_evidence(
            account, runtime_dir, epoch
        )
        from .stress90_operator_continuity import (
            require_stress90_operator_continuity_authority,
        )
        from .trading_day_evidence import TradingDayEvidenceStore

        current_tde = TradingDayEvidenceStore(
            runtime_dir / "ctp_trading_day_evidence.json"
        ).load_required()
        require_stress90_operator_continuity_authority(
            record,
            registry_evidence=registry_evidence,
            trading_day_evidence=current_tde,
        )
        binding_current = True
        facts["target_trading_day_evidence_sequence"] = current_tde.sequence
        facts["target_trading_day_evidence_checksum"] = current_tde.checksum
    except (OSError, RuntimeError) as exc:
        binding_current = False
        facts["binding_error"] = str(exc)
    facts["binding_current"] = binding_current
    state_current = bool(
        runtime_state is not None
        and runtime_state.trading_day == evidence.target_ctp_trading_day
        and runtime_state.last_account_trading_day == evidence.target_ctp_trading_day
    )
    facts["state_current"] = state_current
    requires_rebase = bool(
        runtime_state is not None
        and (
            float(runtime_state.last_account_deposit) != 0.0
            or float(runtime_state.last_account_withdrawal) != 0.0
        )
    )
    requires_roll = bool(not requires_rebase and not (binding_current and state_current))

    permit_current = False
    try:
        permit = Stress90ActivationPermitStore(
            runtime_dir / "stress90_activation_permit.json"
        ).load_record()
    except (OSError, RuntimeError) as exc:
        facts["permit_error"] = str(exc)
    else:
        permit_current = bool(
            permit is not None
            and permit.permit.status == "issued"
            and permit.permit.evidence.account_continuity_mode == "operator_managed"
            and permit.permit.evidence.operator_continuity_receipt_digest == record.checksum
        )
    facts["technical_permit_current"] = permit_current
    passed = bool(binding_current and state_current)
    detail = (
        "operator trust receipt is current for account/runtime/epoch"
        if passed
        else "operator trust receipt is missing or stale for current account/runtime state"
    )
    return facts, requires_roll, requires_rebase, not permit_current, passed, detail


def _add_stress90_local_status(
    report: OperationalReport,
    config,
    runtime_state: RuntimeState | None,
) -> None:
    if not config.directional.enabled or config.directional.policy != "stress90":
        return

    from .directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
    )
    from .directional_stress90_oi_runtime import (
        OiEvidenceIntegrityError,
        Stress90OiEvidenceStore,
    )
    from .directional_stress90_policy import (
        STRESS90_POLICY,
        canonical_stress90_digest,
    )
    from .directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90SeedStore,
        Stress90StateIntegrityError,
        completed_account_drawdown,
        drawdown_reserve_triggered_from_state,
    )

    runtime_dir = Path(config.state_path).parent
    policy_path = runtime_dir / "stress90_policy_state.json"
    seed_path = runtime_dir / "stress90_bootstrap_seed.json"
    oi_path = runtime_dir / "stress90_oi_evidence.json"
    intent_path = runtime_dir / "stress90_execution_intent.json"
    lifecycle_path = runtime_dir / "stress90_lifecycle_transaction.json"
    account_registry_path = Path(
        str(
            getattr(
                config,
                "account_registry_path",
                runtime_dir / ".account-runtime-registry.json",
            )
        )
    )
    blockers: list[str] = []
    facts: dict[str, object] = {
        "policy_id": STRESS90_POLICY.policy_id,
        "policy_version": STRESS90_POLICY.definition_version,
        "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
        "policy_manifest_digest": STRESS90_POLICY.policy_manifest_digest,
        "historical_candidate_weight_sha256": (STRESS90_POLICY.historical_candidate_weight_sha256),
        "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
        "bootstrap_source_manifest": None,
        "bootstrap_through_day": None,
        "last_completed_target_day": None,
        "current_ctp_trading_day": (
            runtime_state.trading_day if runtime_state is not None else None
        ),
        "required_signal_day": None,
        "required_activity_day": None,
        "required_oi_day": None,
        "ohlc_latest_day": None,
        "ohlc_digest": None,
        "oi_latest_complete_day": None,
        "oi_latest_digest": None,
        "last_decision_digest": None,
        "last_base_decision_digest": None,
        "last_oi_decision_digest": None,
        "last_cost_decision_digest": None,
        "last_survivor_decision_digest": None,
        "current_hhi": None,
        "prior_hhi_median": None,
        "concentration_freeze": None,
        "completed_account_wealth": None,
        "completed_account_high_watermark": None,
        "completed_drawdown": None,
        "drawdown_reserve_freeze": None,
        "pending_decision": None,
        "target_lots": None,
        "current_local_lots": None,
        "current_broker_lots": None,
        "target_gross": None,
        "actual_gross": None,
        "integer_tracking_error": None,
        "policy_data_gap": True,
        "live_eligibility": False,
        "capital_activation_eligible": False,
        "external_activation_gates": dict(_STRESS90_EXTERNAL_ACTIVATION_GATE_STATUS),
        "external_blocker_reasons": list(STRESS90_EXTERNAL_ACTIVATION_GATES),
        "remaining_blocker_reasons": blockers,
        "policy_state_path": str(policy_path),
        "seed_path": str(seed_path),
        "oi_evidence_path": str(oi_path),
        "execution_intent_path": str(intent_path),
        "lifecycle_transaction_path": str(lifecycle_path),
        "account_runtime_registry_path": str(account_registry_path),
    }
    configured_risk = {
        "max_target_gross": float(config.directional.max_gross_leverage),
        "max_realized_gross": float(config.directional.max_gross_leverage),
        "max_margin_ratio": float(config.risk.max_margin_ratio),
        "min_available_ratio": float(config.risk.min_available_ratio),
        "max_daily_loss_ratio": float(config.risk.max_daily_loss_ratio),
        "max_total_drawdown_ratio": float(config.risk.max_total_drawdown_ratio),
        "max_contract_lots": min(
            int(config.directional.max_contract_volume),
            int(config.risk.max_contract_volume),
        ),
        "margin_estimate_buffer": float(config.risk.margin_estimate_buffer),
    }
    historical_risk = dict(STRESS90_POLICY.hard_risk_envelope)
    (
        operator_continuity,
        requires_operator_roll_forward,
        requires_account_rebase,
        requires_new_permit,
        continuity_ready,
        continuity_detail,
    ) = _stress90_account_continuity_status(config, runtime_state)
    facts.update(
        account_continuity_mode=str(
            getattr(config.directional, "account_continuity_mode", "strict")
        ),
        operator_continuity=operator_continuity,
        requires_operator_roll_forward=requires_operator_roll_forward,
        requires_account_rebase=requires_account_rebase,
        requires_new_permit=requires_new_permit,
        external_activation_gates_completed=False,
    )
    if facts["account_continuity_mode"] == "operator_managed":
        report.add(
            "stress90_operator_continuity",
            continuity_ready,
            continuity_detail,
        )
        if not continuity_ready:
            blockers.append("stress90_operator_continuity")
    facts["configured_hard_risk_envelope"] = configured_risk
    facts["historical_hard_risk_envelope"] = historical_risk
    facts["commissioning_risk_differences"] = {
        name: {"historical": historical_risk[name], "configured": value}
        for name, value in configured_risk.items()
        if value != historical_risk[name]
    }

    try:
        from .stress90_lifecycle_transaction import Stress90LifecycleTransactionStore

        lifecycle = Stress90LifecycleTransactionStore(lifecycle_path).load()
    except (OSError, RuntimeError) as exc:
        blockers.append("stress90_lifecycle_transaction")
        facts["lifecycle_transaction"] = {"valid": False, "error": str(exc)}
        report.add("stress90_lifecycle_transaction", False, str(exc))
    else:
        pending_lifecycle = bool(lifecycle is not None and lifecycle.status == "prepared")
        facts["lifecycle_transaction"] = (
            None
            if lifecycle is None
            else {
                "transaction_id": lifecycle.transaction_id,
                "operation": lifecycle.operation,
                "status": lifecycle.status,
                "trading_day": lifecycle.trading_day,
            }
        )
        if pending_lifecycle:
            blockers.append("stress90_lifecycle_transaction")
        report.add(
            "stress90_lifecycle_transaction",
            not pending_lifecycle,
            "no lifecycle transaction is pending"
            if not pending_lifecycle
            else "lifecycle transaction is pending exact CLI roll-forward",
        )

    seed = None
    try:
        seed = Stress90SeedStore(seed_path).load_required()
    except (OSError, Stress90StateIntegrityError) as exc:
        blockers.append("stress90_seed")
        report.add("stress90_seed_integrity", False, str(exc))
    else:
        facts["bootstrap_source_manifest"] = dict(seed.bootstrap_source_manifest)
        facts["bootstrap_through_day"] = seed.bootstrap_through_day
        report.add("stress90_seed_integrity", True, "immutable Stress-90 seed verified")

    policy_state = None
    try:
        policy_record = Stress90PolicyStateStore(policy_path).load_required_record()
        policy_state = policy_record.state
    except (OSError, Stress90StateIntegrityError) as exc:
        blockers.append("stress90_policy_state")
        facts["policy_state_valid"] = False
        facts["policy_state_error"] = str(exc)
        report.add("stress90_policy_state_integrity", False, str(exc))
    else:
        prepared = policy_state.prepared_decision
        facts.update(
            policy_state_valid=True,
            policy_state_sequence=policy_record.sequence,
            last_completed_target_day=policy_state.last_completed_target_day,
            required_signal_day=(
                prepared.input_days["completed_close"]
                if prepared is not None
                else policy_state.last_completed_target_day
            ),
            required_activity_day=(
                prepared.previous_target_trading_day
                if prepared is not None
                else policy_state.last_completed_target_day
            ),
            required_oi_day=(
                prepared.input_days["completed_oi"]
                if prepared is not None
                else policy_state.last_completed_target_day
            ),
            last_decision_digest=policy_state.last_decision_digest,
            last_base_decision_digest=(
                prepared.layer_digests.get("base") if prepared is not None else None
            ),
            last_oi_decision_digest=(
                prepared.layer_digests.get("oi")
                if prepared is not None
                else canonical_stress90_digest(dict(policy_state.last_oi_confirmed_weights))
            ),
            last_cost_decision_digest=(
                prepared.layer_digests.get("cost")
                if prepared is not None
                else canonical_stress90_digest(dict(policy_state.last_cost_approved_weights))
            ),
            last_survivor_decision_digest=(
                prepared.layer_digests.get("survivor")
                if prepared is not None
                else canonical_stress90_digest(dict(policy_state.last_survivor_weights))
            ),
            current_hhi=(
                prepared.current_hhi
                if prepared is not None
                else (
                    policy_state.completed_concentrations[-1]
                    if policy_state.completed_concentrations
                    else None
                )
            ),
            prior_hhi_median=(prepared.prior_hhi_median if prepared is not None else None),
            concentration_freeze=(prepared.concentration_freeze if prepared is not None else None),
            completed_account_wealth=policy_state.completed_account_wealth,
            completed_account_high_watermark=(policy_state.completed_account_high_watermark),
            completed_drawdown=completed_account_drawdown(policy_state),
            drawdown_reserve_freeze=drawdown_reserve_triggered_from_state(policy_state),
            pending_decision=(
                {
                    "target_trading_day": prepared.target_trading_day,
                    "daily_decision_digest": prepared.daily_decision_digest,
                }
                if prepared is not None
                else None
            ),
            target_gross=(
                sum(abs(float(value)) for value in prepared.survivor_weights.values())
                if prepared is not None
                else sum(abs(float(value)) for value in policy_state.last_survivor_weights.values())
            ),
        )
        raw_product_weights = (
            dict(prepared.survivor_weights)
            if prepared is not None
            else dict(policy_state.last_survivor_weights)
        )
        raw_target_gross = sum(abs(float(value)) for value in raw_product_weights.values())
        facts["raw_target_gross"] = raw_target_gross
        facts["scaled_target_gross"] = raw_target_gross * float(config.directional.live_risk_scale)
        facts["raw_product_weights"] = raw_product_weights
        facts["scaled_product_weights"] = {
            product: float(weight) * float(config.directional.live_risk_scale)
            for product, weight in raw_product_weights.items()
        }
        identity_ok = bool(
            seed is not None
            and policy_state.bootstrap_seed_digest == seed.seed_digest
            and policy_state.bootstrap_source_manifest == seed.bootstrap_source_manifest
        )
        if not identity_ok:
            blockers.append("stress90_seed_state_identity")
        report.add(
            "stress90_seed_state_identity",
            identity_ok,
            "policy state matches immutable seed"
            if identity_ok
            else "policy state does not match immutable seed",
        )
        report.add(
            "stress90_policy_state_integrity",
            True,
            f"Stress-90 state sequence {policy_record.sequence} verified",
        )

    try:
        from .account_runtime_registry import AccountRuntimeRegistry

        if (
            policy_state is None
            or policy_state.live_account_identity_digest is None
            or policy_state.live_account_epoch is None
        ):
            raise RuntimeError("Stress-90 policy account identity/epoch is missing")
        registry_evidence = AccountRuntimeRegistry(account_registry_path).require_binding_evidence(
            policy_state.live_account_identity_digest,
            runtime_dir.resolve(strict=False),
            policy_state.live_account_epoch,
        )
    except (OSError, RuntimeError) as exc:
        blockers.append("stress90_account_runtime_registry")
        facts["account_runtime_registry"] = {
            "valid": False,
            "path": str(account_registry_path),
            "error": str(exc),
        }
        report.add("stress90_account_runtime_registry", False, str(exc))
    else:
        facts["account_runtime_registry"] = {
            "valid": True,
            "path": str(account_registry_path),
            "sequence": registry_evidence.registry_sequence,
            "checksum": registry_evidence.registry_checksum,
            "runtime_identity_digest": (registry_evidence.binding.runtime_identity_digest),
            "account_epoch": registry_evidence.binding.account_epoch,
        }
        report.add(
            "stress90_account_runtime_registry",
            True,
            "machine account/runtime/epoch binding verified",
        )

    ohlc = report.facts.get("directional_ohlc_cache")
    if isinstance(ohlc, dict) and ohlc.get("valid") is True:
        facts["ohlc_latest_day"] = str(ohlc.get("latest_date", "")).replace("-", "")
        facts["ohlc_digest"] = ohlc.get("content_digest")
    else:
        blockers.append("directional_ohlc_cache")

    try:
        oi_record = Stress90OiEvidenceStore(oi_path).load_required_record()
    except (OSError, OiEvidenceIntegrityError) as exc:
        blockers.append("stress90_oi_evidence")
        report.add("stress90_oi_evidence_integrity", False, str(exc))
    else:
        completed = oi_record.state.completed
        latest = completed[-1] if completed else None
        facts["oi_evidence_sequence"] = oi_record.sequence
        facts["oi_latest_complete_day"] = (
            latest.trading_day if latest is not None and latest.complete else None
        )
        facts["oi_latest_digest"] = latest.evidence_digest if latest is not None else None
        facts["oi_coverage"] = (
            {
                "expected": {
                    product: list(latest.expected_contracts[product])
                    for product in STRESS90_POLICY.oi_products
                },
                "received": list(latest.received_contracts),
                "missing": list(latest.missing_contracts),
                "complete": latest.complete,
            }
            if latest is not None
            else None
        )
        oi_ready = bool(latest is not None and latest.complete)
        if not oi_ready:
            blockers.append("stress90_oi_evidence")
        report.add(
            "stress90_oi_evidence_integrity",
            oi_ready,
            "completed Stress-90 OI evidence verified"
            if oi_ready
            else "no complete Stress-90 OI evidence is available",
        )

    try:
        intent_record = Stress90ExecutionIntentStore(intent_path).load_record()
    except (OSError, Stress90ExecutionIntentIntegrityError) as exc:
        blockers.append("stress90_execution_intent")
        report.add("stress90_execution_intent_integrity", False, str(exc))
    else:
        intent_matches_policy = bool(
            intent_record is None
            or (
                policy_state is not None
                and intent_record.effective_account_identity_digest
                == policy_state.live_account_identity_digest
                and intent_record.effective_account_epoch == policy_state.live_account_epoch
            )
        )
        if not intent_matches_policy:
            blockers.append("stress90_execution_intent_lifecycle")
        report.add(
            "stress90_execution_intent_integrity",
            intent_matches_policy,
            "no execution intent is prepared"
            if intent_record is None
            else (
                f"execution intent sequence {intent_record.sequence} verified"
                if intent_matches_policy
                else "execution intent account identity/epoch does not match policy state"
            ),
        )
        if intent_record is not None:
            facts["target_lots"] = (
                {}
                if intent_record.retired
                else dict(intent_record.intent.initial_margin_fitted_lots)
            )
            facts["raw_integer_lots"] = None
            facts["scaled_integer_lots"] = None
            facts["margin_fitted_lots"] = (
                {}
                if intent_record.retired
                else dict(intent_record.intent.initial_margin_fitted_lots)
            )
            facts["final_lots"] = (
                {} if intent_record.retired else dict(intent_record.intent.freeze_authorized_lots)
            )
            facts["live_plan_metrics_available"] = False
            facts["live_plan_metrics_reason"] = (
                "fresh CTP quotes/specs are required; run doctor or stress90-capacity-report"
            )
            facts["integer_tracking_error"] = None
            facts["live_cost_compatibility"] = None
            facts["estimated_margin_ratio"] = None
            facts["estimated_available_ratio"] = None
            if intent_record.retired:
                assert intent_record.retirement is not None
                facts["execution_intent"] = {
                    "retired": True,
                    "retirement_transaction_id": (intent_record.retirement.transaction_id),
                    "account_identity_digest": (intent_record.effective_account_identity_digest),
                    "account_epoch": intent_record.effective_account_epoch,
                }
            else:
                facts["execution_intent"] = {
                    "retired": False,
                    "target_trading_day": intent_record.intent.target_trading_day,
                    "daily_decision_digest": intent_record.intent.daily_decision_digest,
                    "authorized_transition_products": list(
                        intent_record.intent.authorized_transition_products
                    ),
                }

    if runtime_state is None:
        blockers.append("runtime_state")
    else:
        try:
            local_positions = StateStore(config.state_path).positions_from_state(runtime_state)
        except (TypeError, ValueError):
            blockers.append("runtime_positions")
        else:
            facts["current_local_lots"] = {
                position.symbol: position.net_volume
                for position in local_positions
                if not position.empty
            }
    try:
        if runtime_state is None or seed is None:
            raise RuntimeError("runtime state and immutable bootstrap seed are required")
        from .directional_policy_activation import (
            POLICY_IDENTITY_STATE_KEY,
            require_directional_policy_identity,
        )
        from .stress90_risk_overlay import stress90_risk_overlay_digest

        configured_risk_overlay_digest = stress90_risk_overlay_digest(
            config.directional, config.risk
        )
        marker = runtime_state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)
        bound_risk_overlay_digest = (
            str(marker.get("risk_overlay_digest", "")) if isinstance(marker, dict) else ""
        )
        risk_overlay_matches = bound_risk_overlay_digest == configured_risk_overlay_digest
        facts["configured_live_risk_scale"] = float(config.directional.live_risk_scale)
        facts["risk_overlay_digest"] = configured_risk_overlay_digest
        facts["bound_risk_overlay_digest"] = bound_risk_overlay_digest
        facts["risk_overlay_matches"] = risk_overlay_matches
        facts["risk_overlay_reactivation_required"] = not risk_overlay_matches
        report.add(
            "stress90_risk_overlay_identity",
            risk_overlay_matches,
            "configured risk overlay matches activated runtime identity"
            if risk_overlay_matches
            else "configured risk overlay differs from activated runtime; HALTED reactivation required",
        )
        if not risk_overlay_matches:
            blockers.append("stress90_risk_overlay_identity")
        require_directional_policy_identity(
            runtime_state,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest=seed.seed_digest,
            risk_overlay_digest=configured_risk_overlay_digest,
        )
    except RuntimeError as exc:
        blockers.append("stress90_runtime_policy_identity")
        facts["runtime_policy_identity"] = {
            "valid": False,
            "error": str(exc),
        }
        report.add("stress90_runtime_policy_identity", False, str(exc))
    else:
        marker = runtime_state.strategy_states[POLICY_IDENTITY_STATE_KEY]
        facts["runtime_policy_identity"] = {"valid": True, **dict(marker)}
        report.add(
            "stress90_runtime_policy_identity",
            True,
            "runtime state matches Stress-90 definition, products and bootstrap seed",
        )
    blockers.append("live_broker_snapshot_unverified")
    facts["remaining_blocker_reasons"] = list(dict.fromkeys(blockers))
    facts["policy_data_gap"] = bool(blockers)
    facts["live_eligibility"] = False
    report.facts["stress90"] = facts


def _add_stress90_ctp_order_journal_status(report: OperationalReport, config) -> None:
    if not config.directional.enabled or config.directional.policy != "stress90":
        return
    from .broker.ctp_order_journal import (
        CtpOrderJournalIntegrityError,
        CtpOrderSubmissionJournal,
    )

    path = Path(config.state_path).parent / "stress90_ctp_orders.json"
    previous_path = path.with_name(path.name + ".prev")
    journal = CtpOrderSubmissionJournal(path)
    durable_evidence_present = bool(
        path.exists()
        or previous_path.exists()
        or journal.epoch_manifest_path.exists()
        or journal.epoch_manifest_previous_path.exists()
        or journal.epoch_root.exists()
        or any(path.parent.glob(f"{path.name}.archive.*.json"))
        or any(path.parent.glob(f"{path.name}.runtime-index.*.json"))
    )
    facts: dict[str, object] = {
        "path": str(path),
        "present": path.exists(),
        "durable_evidence_present": durable_evidence_present,
    }
    try:
        audit = journal.audit_epochs() if durable_evidence_present else None
    except (OSError, CtpOrderJournalIntegrityError) as exc:
        facts.update({"valid": False, "error": str(exc)})
        report.add("stress90_ctp_order_journal_integrity", False, str(exc))
    else:
        record = None if audit is None else audit.current_record
        manifest = None if audit is None else audit.manifest
        entries = () if record is None else record.all_entries
        unresolved_entries = tuple(
            entry for entry in entries if entry.status not in {"terminal", "aborted_before_send"}
        )
        fill_count = sum(len(entry.fill_keys) for entry in entries)
        reserved_remaining = sum(
            entry.request.volume - entry.filled_volume
            for entry in entries
            if entry.status != "aborted_before_send"
        )
        identity_source = entries[0] if entries else None
        facts.update(
            {
                "valid": True,
                "empty": record is None,
                "sequence": 0 if record is None else record.sequence,
                "generation_digest": "" if record is None else record.checksum,
                "entry_count": len(entries),
                "unresolved_entry_count": len(unresolved_entries),
                "hot_entry_count": 0 if record is None else len(record.entries),
                "archive_entry_count": (0 if record is None else record.archive_entry_count),
                "archive_head_digest": (
                    "" if record is None else (record.archive_head_checksum or "")
                ),
                "runtime_index_digest": (
                    "" if record is None else (record.runtime_index_checksum or "")
                ),
                "order_identity_count": len(entries),
                "order_identity_bloom_digest": (
                    "" if record is None else (record.order_identity_bloom_digest or "")
                ),
                "fill_identity_count": fill_count,
                "reserved_remaining_fill_slots": reserved_remaining,
                "sealed_epoch_count": 0 if audit is None else audit.sealed_epoch_count,
                "sealed_entry_count": 0 if audit is None else audit.sealed_entry_count,
                "sealed_fill_identity_count": (
                    0 if audit is None else audit.sealed_fill_identity_count
                ),
                "epoch_manifest_digest": "" if manifest is None else manifest.checksum,
                "current_epoch_account_identity_digest": (
                    "" if manifest is None else manifest.current_account_identity_digest
                ),
                "account_identity_digest": (
                    "" if identity_source is None else identity_source.account_identity_digest
                ),
                "policy_id": "" if identity_source is None else identity_source.policy_id,
                "policy_definition_digest": (
                    "" if identity_source is None else identity_source.policy_definition_digest
                ),
                "products_manifest_digest": (
                    "" if identity_source is None else identity_source.products_manifest_digest
                ),
            }
        )
        report.add(
            "stress90_ctp_order_journal_integrity",
            True,
            (
                "no CTP order journal evidence exists"
                if audit is None
                else "full CTP order journal cold archive chain verified"
            ),
        )
        report.add(
            "stress90_ctp_order_journal_lifecycle_ready",
            not unresolved_entries,
            (
                "all durable CTP order submissions are terminal"
                if not unresolved_entries
                else "durable CTP order submissions remain unresolved"
            ),
        )
    raw_stress = report.facts.get("stress90")
    stress = raw_stress if isinstance(raw_stress, dict) else {}
    stress["ctp_order_journal"] = facts
    report.facts["stress90"] = stress


def build_local_status(
    config,
    *,
    min_free_bytes: int = MIN_OPERATIONAL_DISK_FREE_BYTES,
) -> OperationalReport:
    """Inspect local state and evidence without creating files or directories."""
    report = OperationalReport()
    store = StateStore(config.state_path)
    state_present = store.path.exists()
    state: RuntimeState | None = None
    if state_present:
        try:
            state = store.load()
        except (OSError, StateIntegrityError) as exc:
            report.add("state_integrity", False, str(exc))
            report.facts["state"] = {
                "present": True,
                "valid": False,
                "error": str(exc),
            }
        else:
            report.add("state_integrity", True, "current state envelope verified")
            report.facts["state"] = {"present": True, **_state_facts(state)}
    else:
        try:
            store.load()
        except (OSError, StateIntegrityError) as exc:
            report.add("state_integrity", False, str(exc))
            report.facts["state"] = {
                "present": False,
                "valid": False,
                "error": str(exc),
            }
        else:
            report.add("state_integrity", True, "no current state file or incident evidence")
            report.facts["state"] = {"present": False, "valid": None}

    _add_directional_ohlc_cache_status(report, config)
    _add_stress90_local_status(report, config, state)
    _add_stress90_ctp_order_journal_status(report, config)

    previous: dict[str, object] = {
        "path": str(store.previous_path),
        "present": store.previous_path.exists(),
        "valid": None,
    }
    if store.previous_path.exists():
        try:
            previous_state = store.load_previous()
        except (OSError, StateIntegrityError) as exc:
            previous["valid"] = False
            previous["error"] = str(exc)
            report.warnings.append(f"previous state evidence is invalid: {exc}")
        else:
            if previous_state is not None:
                previous.update(_state_facts(previous_state))
    report.facts["previous_state"] = previous

    paths = {
        "state": Path(config.state_path),
        "log": Path(config.log_path),
        "report": Path(config.report_path),
        "audit": Path(config.journal_path),
        "alert": Path(config.alert_path),
        "directional_ohlc_cache": Path(config.state_path).with_name("directional_ohlc_cache.json"),
    }
    if config.directional.enabled and config.directional.policy == "stress90":
        runtime_dir = Path(config.state_path).parent
        paths.update(
            {
                "directional_activity": runtime_dir / "directional_activity.json",
                "stress90_policy_state": runtime_dir / "stress90_policy_state.json",
                "stress90_seed": runtime_dir / "stress90_bootstrap_seed.json",
                "stress90_oi_evidence": runtime_dir / "stress90_oi_evidence.json",
                "stress90_execution_intent": runtime_dir / "stress90_execution_intent.json",
                "stress90_ctp_order_journal": runtime_dir / "stress90_ctp_orders.json",
                "account_runtime_registry": Path(
                    str(
                        getattr(
                            config,
                            "account_registry_path",
                            runtime_dir / ".account-runtime-registry.json",
                        )
                    )
                ),
            }
        )
    report.facts["paths"] = {name: _path_facts(path) for name, path in paths.items()}
    ancestors = {_existing_ancestor(path.parent) for path in paths.values()}
    symlink_paths = sorted(
        {str(candidate) for path in paths.values() for candidate in _symlink_components(path)}
    )
    invalid_targets = sorted(
        str(path)
        for path in paths.values()
        if not path.is_symlink() and path.exists() and not path.is_file()
    )
    invalid_ancestors = sorted(str(path) for path in ancestors if not path.is_dir())
    inaccessible_ancestors = sorted(
        str(path) for path in ancestors if path.is_dir() and not os.access(path, os.W_OK | os.X_OK)
    )
    write_targets = (paths["log"], paths["report"], paths["audit"], paths["alert"])
    unwritable_targets = sorted(
        str(path) for path in write_targets if path.is_file() and not os.access(path, os.W_OK)
    )
    path_errors = [
        *(f"symlink is not allowed in runtime path: {path}" for path in symlink_paths),
        *(f"target is not a file: {path}" for path in invalid_targets),
        *(f"ancestor is not a directory: {path}" for path in invalid_ancestors),
        *(f"ancestor is not writable/searchable: {path}" for path in inaccessible_ancestors),
        *(f"write target is not writable: {path}" for path in unwritable_targets),
    ]
    report.add(
        "runtime_paths_writable",
        not path_errors,
        "runtime path ancestors are writable" if not path_errors else "; ".join(path_errors),
    )
    disk_roots = {path if path.is_dir() else path.parent for path in ancestors}
    free_by_path = {str(path): shutil.disk_usage(path).free for path in sorted(disk_roots)}
    minimum_free = min(free_by_path.values()) if free_by_path else 0
    report.facts["disk"] = {
        "minimum_free_bytes": minimum_free,
        "required_free_bytes": min_free_bytes,
        "free_bytes_by_path": free_by_path,
    }
    report.add(
        "disk_space",
        minimum_free >= min_free_bytes,
        f"minimum free bytes {minimum_free}; required {min_free_bytes}",
    )
    return report


def _live_specs_valid(specs: dict[str, ContractSpec], symbols: list[str]) -> tuple[bool, str]:
    missing = sorted(set(symbols).difference(specs))
    if missing:
        return False, "missing live metadata: " + ", ".join(missing)
    for symbol in symbols:
        spec = specs[symbol]
        values = (spec.multiplier, spec.price_tick, spec.margin_rate_long, spec.margin_rate_short)
        if spec.symbol != symbol or not spec.exchange:
            return False, f"invalid live contract identity: {symbol}"
        if any(not isfinite(value) or value <= 0 for value in values):
            return False, f"invalid positive live contract field: {symbol}"
        if any(
            not isfinite(getattr(spec.fee, name)) or getattr(spec.fee, name) < 0
            for name in spec.fee.__dataclass_fields__
        ):
            return False, f"invalid live contract fee: {symbol}"
    return True, f"verified live metadata for {len(symbols)} sampled contracts"


def _add_stress90_doctor_status(
    report: OperationalReport,
    config,
    *,
    trading_day: str,
    account: AccountSnapshot,
    positions: list[ContractPosition],
    active_order_count: int,
    catalog: list[ContractInfo],
    requested_symbols: list[str],
    metadata: dict[str, ContractSpec],
    quotes: dict[str, Tick],
    session_trade_ownership_valid: bool,
    session_trade_ownership_detail: str,
) -> None:
    if not config.directional.enabled or config.directional.policy != "stress90":
        return
    from .directional_stress90_policy import STRESS90_POLICY
    from .directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90SeedStore,
        Stress90StateIntegrityError,
    )

    raw_stress = report.facts.get("stress90")
    stress: dict[str, object]
    if isinstance(raw_stress, dict):
        stress = raw_stress
    else:  # pragma: no cover - local status contract
        stress = {"remaining_blocker_reasons": []}
        report.facts["stress90"] = stress
    raw_blockers = stress.get("remaining_blocker_reasons")
    blockers = [str(item) for item in raw_blockers] if isinstance(raw_blockers, list) else []
    requested = sorted(set(requested_symbols))
    quote_missing = sorted(set(requested) - set(quotes))
    report.add(
        "stress90_live_quote_coverage",
        bool(requested) and not quote_missing,
        f"verified quotes for {len(requested)} contracts"
        if requested and not quote_missing
        else "missing live quotes: " + ",".join(quote_missing or requested),
    )
    if not requested or quote_missing:
        blockers.append("stress90_live_quotes")

    costs: dict[str, dict[str, object]] = {}
    cost_errors: list[str] = []
    incompatible: list[str] = []
    for symbol in requested:
        tick = quotes.get(symbol)
        spec = metadata.get(symbol)
        if tick is None or spec is None:
            continue
        try:
            estimate = estimate_stress90_contract_cost(
                tick,
                spec,
                hurdle_bps=STRESS90_POLICY.cost_hurdle_bps,
                require_commission_evidence=True,
            )
        except (TypeError, ValueError) as exc:
            cost_errors.append(f"{symbol}: {exc}")
            continue
        costs[symbol] = estimate
        if estimate["historical_15bp_compatible"] is not True:
            incompatible.append(symbol)
    cost_ready = bool(requested) and len(costs) == len(requested) and not incompatible
    cost_detail = (
        f"{len(costs)} live contracts fit the fixed 15bp historical hurdle"
        if cost_ready
        else "; ".join(
            [
                *(cost_errors or []),
                *(
                    ["minimum deterministic cost/slippage exceeds 15bp: " + ",".join(incompatible)]
                    if incompatible
                    else []
                ),
                *(["cost evidence is incomplete"] if len(costs) != len(requested) else []),
            ]
        )
    )
    report.add("stress90_live_cost_compatibility", cost_ready, cost_detail)
    if not cost_ready:
        blockers.append("historical_15bp_cost")
    stress["live_costs"] = costs

    runtime_dir = Path(config.state_path).parent
    try:
        generic_record = StateStore(config.state_path).load_required_record()
    except (OSError, StateIntegrityError) as exc:
        account_path_ready = False
        account_path_detail = f"generic account path is unavailable: {exc}"
    else:
        generic_state = generic_record.state
        persisted_prebalance = (
            float(generic_state.day_start_equity)
            - float(generic_state.last_account_deposit)
            + float(generic_state.last_account_withdrawal)
        )
        account_path_ready = bool(
            account.cash_flow_verified
            and account.settlement_verified
            and account.previous_settlement_equity is not None
            and account.settlement_id is not None
            and generic_state.trading_day == trading_day
            and generic_state.last_account_trading_day == trading_day
            and generic_state.last_account_cash_flow_verified
            and generic_state.last_account_settlement_id == account.settlement_id
            and generic_state.last_account_deposit == account.deposit
            and generic_state.last_account_withdrawal == account.withdrawal
            and persisted_prebalance == account.previous_settlement_equity
        )
        account_path_detail = (
            "persisted settlement/cash-flow account path matches the fresh Broker snapshot"
            if account_path_ready
            else "persisted settlement/cash-flow account path is discontinuous"
        )
    report.add(
        "stress90_account_path_continuity",
        account_path_ready,
        account_path_detail,
    )
    if not account_path_ready:
        blockers.append("stress90_account_path_continuity")
    report.add(
        "stress90_session_trade_ownership",
        session_trade_ownership_valid,
        session_trade_ownership_detail,
    )
    if not session_trade_ownership_valid:
        blockers.append("stress90_session_trade_ownership")
    try:
        policy_state = Stress90PolicyStateStore(
            runtime_dir / "stress90_policy_state.json"
        ).load_required()
    except (OSError, Stress90StateIntegrityError):
        policy_state = None
    if policy_state is not None:
        prepared = policy_state.prepared_decision
        stress["intermediate_targets"] = {
            "base": dict(prepared.base_weights) if prepared is not None else None,
            "oi_confirmed": (dict(prepared.oi_confirmed_weights) if prepared is not None else None),
            "cost_approved": (
                dict(prepared.cost_approved_weights) if prepared is not None else None
            ),
            "survivor": (
                dict(prepared.survivor_weights)
                if prepared is not None
                else dict(policy_state.last_survivor_weights)
            ),
        }
        last_target = policy_state.last_completed_target_day
        continuity = bool(
            len(trading_day) == 8
            and last_target == trading_day
            and prepared is not None
            and prepared.target_trading_day == trading_day
            and prepared.daily_decision_digest == policy_state.last_decision_digest
        )
    else:
        continuity = False
    report.add(
        "stress90_target_day_continuity",
        continuity,
        "Stress-90 persisted and prepared target exactly match current CTP day"
        if continuity
        else "Stress-90 target/current trading-day continuity is untrusted",
    )
    if not continuity:
        blockers.append("stress90_target_day_continuity")

    preview_ready = False
    preview_detail = "no prepared Stress-90 decision is available"
    if policy_state is not None and policy_state.prepared_decision is not None:
        try:
            from .directional_stress90_execution import Stress90ExecutionIntentStore
            from .directional_stress90_planner import build_stress90_rebalance_stages
            from .directional_stress90_state import drawdown_reserve_triggered_from_state

            prepared = policy_state.prepared_decision
            snapshot = DirectionalActivityStore(runtime_dir / "directional_activity.json").load()
            if snapshot is None:
                raise RuntimeError("completed directional activity is unavailable")
            validate_directional_activity_snapshot(snapshot)
            catalog_by_symbol = {item.symbol: item for item in catalog}
            current_lots: dict[str, int] = {}
            preferred: dict[str, str] = {}
            for position in positions:
                if position.empty:
                    continue
                if position.long_total > 0 and position.short_total > 0:
                    raise RuntimeError(
                        f"opposing Broker position is unsupported: {position.symbol}"
                    )
                contract = catalog_by_symbol.get(position.symbol)
                if contract is None:
                    raise RuntimeError(
                        f"Broker position is absent from contract catalog: {position.symbol}"
                    )
                current_lots[position.symbol] = int(position.net_volume)
                preferred[contract.product.upper()] = position.symbol
            selected = select_contracts_from_activity(
                config.directional,
                catalog,
                snapshot,
                datetime.strptime(trading_day, "%Y%m%d").date(),
                preferred_symbols=preferred,
            )
            required_products = {
                product
                for product, weight in prepared.survivor_weights.items()
                if abs(float(weight)) > 1e-15
            }
            available_products = {
                product
                for product in required_products
                if product in selected
                and selected[product].symbol in quotes
                and selected[product].symbol in metadata
            }
            unavailable_products = required_products - available_products
            product_ticks = {
                product: quotes[selected[product].symbol] for product in available_products
            }
            symbol_products = {
                symbol: catalog_by_symbol[symbol].product.upper() for symbol in current_lots
            }
            symbol_products.update(
                {selected[product].symbol: product for product in available_products}
            )
            intent_record = Stress90ExecutionIntentStore(
                runtime_dir / "stress90_execution_intent.json"
            ).load_record()
            intent = (
                None if intent_record is None or intent_record.retired else intent_record.intent
            )
            persisted_lots = None
            persisted_freeze_lots = None
            authorized_products: tuple[str, ...] = ()
            authorized_transition_kinds = None
            if intent is not None:
                if (
                    intent.target_trading_day != prepared.target_trading_day
                    or intent.daily_decision_digest != prepared.daily_decision_digest
                ):
                    raise RuntimeError("execution intent disagrees with prepared decision")
                persisted_lots = intent.initial_margin_fitted_lots
                persisted_freeze_lots = intent.freeze_authorized_lots
                authorized_products = intent.authorized_transition_products
                authorized_transition_kinds = intent.authorized_transition_kinds
            stages = build_stress90_rebalance_stages(
                account=account,
                product_weights=prepared.survivor_weights,
                product_ticks=product_ticks,
                specs=metadata,
                live_risk_scale=config.directional.live_risk_scale,
                incumbent_ticks={
                    symbol: quotes[symbol] for symbol in current_lots if symbol in quotes
                },
                current_lots=current_lots,
                symbol_products=symbol_products,
                max_contract_volume=min(
                    config.directional.max_contract_volume,
                    config.risk.max_contract_volume,
                ),
                max_gross_leverage=config.directional.max_gross_leverage,
                max_margin_ratio=config.risk.max_margin_ratio,
                min_available_ratio=config.risk.min_available_ratio,
                max_daily_loss_ratio=config.risk.max_daily_loss_ratio,
                margin_estimate_buffer=config.risk.margin_estimate_buffer,
                completed_returns=policy_state.recent_daily_returns_for_adaptive_margin,
                drawdown_reserve_freeze=drawdown_reserve_triggered_from_state(policy_state),
                concentration_freeze=prepared.concentration_freeze,
                unavailable_products=unavailable_products,
                authorized_transition_products=authorized_products,
                authorized_transition_kinds=authorized_transition_kinds,
                persisted_margin_fitted_lots=persisted_lots,
                persisted_freeze_authorized_lots=persisted_freeze_lots,
            )
            ticks_by_symbol = {tick.symbol: tick for tick in quotes.values()}

            def realized_weights(lots: dict[str, int]) -> dict[str, float]:
                weights = {product: 0.0 for product in STRESS90_POLICY.products}
                for symbol, volume in lots.items():
                    tick = ticks_by_symbol.get(symbol)
                    spec = metadata.get(symbol)
                    product = symbol_products.get(symbol)
                    if tick is None or spec is None or product is None:
                        continue
                    weights[product] += (
                        float(volume)
                        * float(tick.mid_price)
                        * float(spec.multiplier)
                        / float(account.equity)
                    )
                return weights

            from .models import Offset, OrderRequest, OrderSide
            from .risk import RiskManager
            from .stress90_risk_overlay import (
                scale_stress90_product_weights,
                stress90_risk_overlay_digest,
            )

            raw_product_weights = {
                product: float(prepared.survivor_weights[product])
                for product in STRESS90_POLICY.products
            }
            scaled_product_weights = scale_stress90_product_weights(
                raw_product_weights, config.directional.live_risk_scale
            )
            target_weights = realized_weights(stages.final_frozen_lots)
            actual_weights = realized_weights(current_lots)
            target_gross = sum(abs(value) for value in target_weights.values())
            actual_gross = sum(abs(value) for value in actual_weights.values())
            raw_target_gross = sum(abs(value) for value in raw_product_weights.values())
            scaled_target_gross = sum(abs(value) for value in scaled_product_weights.values())
            tracking_error = sum(
                abs(target_weights[product] - scaled_product_weights[product])
                for product in STRESS90_POLICY.products
            )
            nonzero_weights = {
                product: value for product, value in target_weights.items() if abs(value) > 1e-15
            }
            largest_share = (
                max(abs(value) for value in nonzero_weights.values()) / target_gross
                if target_gross > 0.0
                else 0.0
            )
            product_hhi = (
                sum((abs(value) / target_gross) ** 2 for value in nonzero_weights.values())
                if target_gross > 0.0
                else 0.0
            )
            per_lot_margin: dict[str, float] = {}
            for symbol, volume in stages.final_frozen_lots.items():
                tick = ticks_by_symbol[symbol]
                spec = metadata[symbol]
                rate = spec.margin_rate_long if volume > 0 else spec.margin_rate_short
                per_lot_margin[symbol] = (
                    float(tick.mid_price)
                    * float(spec.multiplier)
                    * float(rate)
                    * float(config.risk.margin_estimate_buffer)
                )
            target_margin = sum(
                abs(volume) * per_lot_margin[symbol]
                for symbol, volume in stages.final_frozen_lots.items()
            )
            estimated_margin_ratio = target_margin / float(account.equity)
            estimated_available_ratio = max(0.0, float(account.equity) - target_margin) / float(
                account.equity
            )
            opening_requests: list[OrderRequest] = []
            for symbol, delta in stages.openings.items():
                tick = ticks_by_symbol[symbol]
                opening_requests.append(
                    OrderRequest(
                        symbol=symbol,
                        exchange=metadata[symbol].exchange,
                        side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
                        offset=Offset.OPEN,
                        volume=abs(int(delta)),
                        price=float(tick.ask_price if delta > 0 else tick.bid_price),
                        reference="stress90-capacity-preview",
                    )
                )
            risk_preview = RiskManager(config.risk).check_open_orders(
                account,
                opening_requests,
                metadata,
                current_contract_volumes={
                    symbol: abs(volume) for symbol, volume in current_lots.items()
                },
            )
            report.add(
                "stress90_risk_manager_preview",
                risk_preview.allowed,
                "RiskManager preview accepts the shared opening plan"
                if risk_preview.allowed
                else "RiskManager preview blocks openings: " + risk_preview.reason,
            )
            cap = min(config.directional.max_contract_volume, config.risk.max_contract_volume)
            contract_cap_hits = sorted(
                symbol
                for symbol, volume in stages.scaled_integer_lots.items()
                if abs(volume) >= cap
            )
            scaled_by_product = {
                symbol_products[symbol]: abs(volume)
                for symbol, volume in stages.scaled_integer_lots.items()
            }
            margin_by_product = {
                symbol_products[symbol]: abs(volume)
                for symbol, volume in stages.margin_fitted_lots.items()
            }
            final_by_product = {
                symbol_products[symbol]: abs(volume)
                for symbol, volume in stages.final_frozen_lots.items()
            }
            clipped_products = {
                "integer": sorted(
                    product
                    for product, weight in scaled_product_weights.items()
                    if abs(weight) > 1e-15 and scaled_by_product.get(product, 0) == 0
                ),
                "margin_or_funding": sorted(
                    product
                    for product, lots in scaled_by_product.items()
                    if margin_by_product.get(product, 0) < lots
                ),
                "drawdown": sorted(
                    {
                        symbol_products[symbol]
                        for symbol in set(stages.margin_fitted_lots)
                        | set(stages.drawdown_frozen_lots)
                        if abs(stages.drawdown_frozen_lots.get(symbol, 0))
                        < abs(stages.margin_fitted_lots.get(symbol, 0))
                    }
                ),
                "concentration": sorted(
                    {
                        symbol_products[symbol]
                        for symbol in set(stages.drawdown_frozen_lots) | set(stages.hhi_frozen_lots)
                        if abs(stages.hhi_frozen_lots.get(symbol, 0))
                        < abs(stages.drawdown_frozen_lots.get(symbol, 0))
                    }
                ),
                "cost": sorted(
                    product
                    for product, contract in selected.items()
                    if contract.symbol in costs
                    and costs[contract.symbol]["historical_15bp_compatible"] is not True
                ),
                "final": sorted(
                    product
                    for product, lots in margin_by_product.items()
                    if final_by_product.get(product, 0) < lots
                ),
            }
            contract_capacity: dict[str, dict[str, object]] = {}
            for product, contract in selected.items():
                tick = quotes.get(contract.symbol)
                spec = metadata.get(contract.symbol)
                if tick is None or spec is None:
                    continue
                notional = float(tick.mid_price) * float(spec.multiplier)
                contract_capacity[product] = {
                    "symbol": contract.symbol,
                    "per_lot_notional": notional,
                    "margin_rate_long": float(spec.margin_rate_long),
                    "margin_rate_short": float(spec.margin_rate_short),
                    "buffered_margin_long": notional
                    * float(spec.margin_rate_long)
                    * float(config.risk.margin_estimate_buffer),
                    "buffered_margin_short": notional
                    * float(spec.margin_rate_short)
                    * float(config.risk.margin_estimate_buffer),
                    **dict(costs.get(contract.symbol, {})),
                }
            representation_parts: list[str] = []
            if clipped_products["integer"]:
                representation_parts.append("scaled products round to zero lots")
            if target_gross == 0.0 and scaled_target_gross > 0.0:
                representation_parts.append(
                    "non-zero scaled portfolio cannot be represented by one-lot granularity"
                )
            if largest_share > 0.5:
                representation_parts.append("final portfolio is dominated by one product")
            if tracking_error > 0.25:
                representation_parts.append("integer target has material L1 tracking error")
            stress.update(
                integer_target_stages={
                    "raw_integer_lots": stages.raw_integer_lots,
                    "scaled_integer_lots": stages.scaled_integer_lots,
                    "margin_fitted_lots": stages.margin_fitted_lots,
                    "drawdown_frozen_lots": stages.drawdown_frozen_lots,
                    "hhi_frozen_lots": stages.hhi_frozen_lots,
                    "final_frozen_lots": stages.final_frozen_lots,
                    "reductions": stages.reductions,
                    "openings": stages.openings,
                    "action_categories": stages.action_categories,
                },
                configured_live_risk_scale=float(config.directional.live_risk_scale),
                risk_overlay_digest=stress90_risk_overlay_digest(config.directional, config.risk),
                raw_product_weights=raw_product_weights,
                scaled_product_weights=scaled_product_weights,
                raw_target_gross=raw_target_gross,
                scaled_target_gross=scaled_target_gross,
                selected_contracts={
                    product: contract.symbol for product, contract in selected.items()
                },
                contract_capacity=contract_capacity,
                target_lots=stages.final_frozen_lots,
                current_broker_lots=current_lots,
                target_gross=target_gross,
                actual_gross=actual_gross,
                integer_tracking_error=tracking_error,
                nonzero_product_count=len(nonzero_weights),
                largest_product_absolute_gross_share=largest_share,
                product_hhi=product_hhi,
                estimated_margin_ratio=estimated_margin_ratio,
                estimated_available_ratio=estimated_available_ratio,
                contract_cap_hits=contract_cap_hits,
                clipped_products=clipped_products,
                risk_manager_preview={
                    "allowed": risk_preview.allowed,
                    "reason": risk_preview.reason,
                },
                execution_chain_commissioning_only=bool(
                    float(config.directional.live_risk_scale) < 1.0
                    or cap < STRESS90_POLICY.max_contract_lots
                    or representation_parts
                ),
                portfolio_representation_warning="; ".join(representation_parts),
                single_product_actual_concentration=(
                    max((abs(value) for value in target_weights.values()), default=0.0)
                    / target_gross
                    if target_gross > 0.0
                    else 0.0
                ),
                unavailable_target_products=sorted(unavailable_products),
            )
            preview_ready = not unavailable_products
            preview_detail = (
                "shared Stress-90 integer planner preview is complete"
                if preview_ready
                else "integer preview has unavailable products: "
                + ",".join(sorted(unavailable_products))
            )
        except (KeyError, OSError, TypeError, ValueError, RuntimeError) as exc:
            preview_detail = f"Stress-90 integer preview is unavailable: {exc}"
    report.add("stress90_integer_preview", preview_ready, preview_detail)
    if not preview_ready:
        blockers.append("stress90_integer_preview")

    state = None
    try:
        state = StateStore(config.state_path).load() if Path(config.state_path).exists() else None
    except (OSError, StateIntegrityError):
        state = None
    marker_present = bool(
        state is not None and "directional_policy_identity" in state.strategy_states
    )
    activation_detail = ""
    if marker_present and state is not None:
        try:
            from .directional_policy_activation import require_directional_policy_identity

            seed = Stress90SeedStore(runtime_dir / "stress90_bootstrap_seed.json").load_required()
            require_directional_policy_identity(
                state,
                policy_id=STRESS90_POLICY.policy_id,
                policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
                products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
                bootstrap_seed_digest=seed.seed_digest,
            )
        except (OSError, RuntimeError, Stress90StateIntegrityError) as exc:
            activation_ready = False
            activation_detail = f"persisted Stress-90 activation identity is invalid: {exc}"
        else:
            activation_ready = bool(state.reconciled and active_order_count == 0)
            activation_detail = (
                "existing Stress-90 activation identity and reconciliation are valid"
                if activation_ready
                else "activated Stress-90 runtime is not reconciled or has active orders"
            )
    else:
        activation_ready = bool(
            state is not None
            and state.runtime_mode == RuntimeMode.HALTED.value
            and state.kill_switch
            and state.reconciled
            and not any(not position.empty for position in positions)
            and active_order_count == 0
        )
        activation_detail = (
            "HALTED, flat, no-active-order and reconcile gates pass"
            if activation_ready
            else "first activation requires HALTED, flat, no active orders and reconcile"
        )
    report.add(
        "stress90_first_activation_gates",
        activation_ready,
        activation_detail,
    )
    if not activation_ready:
        blockers.append("stress90_first_activation_gates")
    runtime_permission = bool(
        marker_present
        and state is not None
        and state.runtime_mode == RuntimeMode.RUNNING.value
        and not state.kill_switch
    )
    report.add(
        "stress90_runtime_permission",
        runtime_permission,
        "Stress-90 identity is activated and runtime permission is RUNNING"
        if runtime_permission
        else "Stress-90 live permission remains HALTED or kill-switched",
    )
    if not runtime_permission:
        blockers.append("stress90_runtime_permission")

    from .stress90_activation_permit import STRESS90_DOCTOR_INTERNAL_P0_CHECKS

    p0_names = STRESS90_DOCTOR_INTERNAL_P0_CHECKS
    failures = [item.name for item in report.checks if item.name in p0_names and not item.passed]
    blockers.extend(failures)
    stress["remaining_blocker_reasons"] = list(dict.fromkeys(blockers))
    stress["stress90_activation_ready"] = activation_ready
    stress["stress90_ready"] = not failures
    external_blockers = list(STRESS90_EXTERNAL_ACTIVATION_GATES)
    stress["external_blocker_reasons"] = external_blockers
    stress["capital_activation_eligible"] = False
    stress["live_eligibility"] = False
    stress["policy_data_gap"] = bool(failures)
    if str(getattr(config.directional, "account_continuity_mode", "strict")) == "operator_managed":
        operator = stress.get("operator_continuity")
        target_day = operator.get("target_ctp_trading_day") if isinstance(operator, dict) else None
        binding_current = bool(
            isinstance(operator, dict) and operator.get("binding_current") is True
        )
        requires_rebase = bool(float(account.deposit) != 0.0 or float(account.withdrawal) != 0.0)
        stress["requires_account_rebase"] = requires_rebase
        stress["requires_operator_roll_forward"] = bool(
            not requires_rebase and (target_day != trading_day or not binding_current)
        )
        stress["external_activation_gates_completed"] = False


def build_doctor_report(
    config,
    *,
    broker_ready: bool,
    fresh_snapshot: bool,
    trading_day: str,
    account: AccountSnapshot,
    positions: list[ContractPosition],
    active_order_count: int,
    catalog: list[ContractInfo],
    requested_symbols: list[str],
    metadata: dict[str, ContractSpec],
    quotes: dict[str, Tick] | None = None,
    session_trade_ownership_valid: bool = False,
    session_trade_ownership_detail: str = "Broker session trade ownership was not proven",
    min_free_bytes: int = MIN_OPERATIONAL_DISK_FREE_BYTES,
) -> OperationalReport:
    """Combine local evidence with an already-fresh CTP snapshot; never place orders."""
    report = build_local_status(config, min_free_bytes=min_free_bytes)
    report.add("broker_ready", broker_ready, "CTP session ready" if broker_ready else "not ready")
    report.add(
        "fresh_snapshot",
        fresh_snapshot,
        "fresh account and complete position snapshot received"
        if fresh_snapshot
        else "fresh snapshot not proven",
    )
    try:
        account.validate()
    except ValueError as exc:
        account_valid = False
        report.add("account_snapshot_valid", False, str(exc))
    else:
        account_valid = True
        report.add("account_snapshot_valid", True, "account values are finite and valid")
    try:
        parsed_trading_day = datetime.strptime(trading_day, "%Y%m%d").strftime("%Y%m%d")
    except ValueError:
        parsed_trading_day = ""
    trading_day_consistent = bool(
        len(trading_day) == 8
        and parsed_trading_day == trading_day
        and account.trading_day == trading_day
    )
    report.add(
        "trading_day_consistent",
        trading_day_consistent,
        f"broker={trading_day!r}, account={account.trading_day!r}",
    )
    report.add(
        "no_active_orders",
        active_order_count == 0,
        f"active order count: {active_order_count}",
    )
    report.add(
        "contract_catalog_available",
        bool(catalog),
        f"contract catalog count: {len(catalog)}",
    )

    requested = sorted(set(requested_symbols))
    live_valid, live_detail = _live_specs_valid(metadata, requested)
    if not requested:
        live_valid, live_detail = False, "no contract metadata was sampled"
    report.add("live_metadata_complete", live_valid, live_detail)
    configured = {
        symbol: config.contracts[symbol] for symbol in requested if symbol in config.contracts
    }
    decision = validate_contract_metadata(configured, metadata) if configured else None
    report.add(
        "configured_metadata_conservative",
        decision is None or decision.allowed,
        "no sampled static contract requires comparison"
        if decision is None
        else decision.reason or "configured metadata does not understate live values",
    )

    store = StateStore(config.state_path)
    state: RuntimeState | None = None
    state_error = ""
    if store.path.exists():
        try:
            state = store.load()
        except (OSError, StateIntegrityError) as exc:
            state_error = str(exc)
    if state is None and store.path.exists():
        report.add("kill_switch_clear", False, f"current state is not trusted: {state_error}")
        report.add("runtime_mode_running", False, f"current state is not trusted: {state_error}")
        report.add("persisted_safety_gates", False, f"current state is not trusted: {state_error}")
        report.add("position_reconciliation", False, "cannot reconcile an untrusted state")
    elif state is None:
        flat = not positions
        report.add("kill_switch_clear", True, "no persisted kill switch")
        report.add("runtime_mode_running", True, "no persisted runtime mode")
        report.add(
            "persisted_safety_gates",
            flat,
            "fresh flat deployment has no persisted gates"
            if flat
            else "broker is not flat but no trusted local state exists",
        )
        report.add(
            "position_reconciliation",
            flat,
            "fresh deployment and broker are flat"
            if flat
            else "broker positions exist without trusted local expected positions",
        )
    else:
        report.add(
            "kill_switch_clear",
            not state.kill_switch,
            state.kill_reason or ("clear" if not state.kill_switch else "kill switch is active"),
        )
        report.add(
            "runtime_mode_running",
            state.runtime_mode == RuntimeMode.RUNNING.value,
            f"runtime mode: {state.runtime_mode}",
        )
        gates_ready = state.reconciled and state.metadata_verified
        report.add(
            "persisted_safety_gates",
            gates_ready,
            f"reconciled={state.reconciled}, metadata_verified={state.metadata_verified}",
        )
        reconciliation = compare_positions(store.positions_from_state(state), positions)
        report.add(
            "position_reconciliation",
            reconciliation.matched,
            reconciliation.details or "local expected positions match broker snapshot",
        )

    risk_failures: list[str] = []
    if not account_valid:
        risk_failures.append("invalid account snapshot")
    elif account.equity > 0:
        margin_ratio = account.margin / account.equity
        available_ratio = account.available / account.equity
        if margin_ratio > config.risk.max_margin_ratio:
            risk_failures.append(
                f"margin ratio {margin_ratio:.6f} > {config.risk.max_margin_ratio:.6f}"
            )
        if available_ratio < config.risk.min_available_ratio:
            risk_failures.append(
                f"available ratio {available_ratio:.6f} < {config.risk.min_available_ratio:.6f}"
            )
        if state is not None:
            if state.trading_day == account.trading_day and state.day_start_equity > 0:
                daily_loss = (
                    max(0.0, state.day_start_equity - account.equity) / state.day_start_equity
                )
                if daily_loss >= config.risk.max_daily_loss_ratio:
                    risk_failures.append(
                        f"daily loss {daily_loss:.6f} >= {config.risk.max_daily_loss_ratio:.6f}"
                    )
            if state.equity_high_watermark > 0:
                drawdown = (
                    max(0.0, state.equity_high_watermark - account.equity)
                    / state.equity_high_watermark
                )
                if drawdown >= config.risk.max_total_drawdown_ratio:
                    risk_failures.append(
                        f"drawdown {drawdown:.6f} >= {config.risk.max_total_drawdown_ratio:.6f}"
                    )
    report.add(
        "account_risk_limits",
        not risk_failures,
        "account margin, available, daily-loss and drawdown limits pass"
        if not risk_failures
        else "; ".join(risk_failures),
    )

    activity_detail = "directional strategy is disabled"
    activity_ready = True
    snapshot = None
    if config.directional.enabled:
        activity_path = Path(config.state_path).with_name("directional_activity.json")
        try:
            snapshot = DirectionalActivityStore(activity_path).load()
            if snapshot is not None:
                validate_directional_activity_snapshot(snapshot)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            activity_ready = False
            activity_detail = f"invalid directional activity evidence: {exc}"
        else:
            if snapshot is None:
                activity_ready = False
                activity_detail = "no completed directional activity; observe one full trading day"
            else:
                try:
                    activity_day = datetime.strptime(snapshot.trading_day, "%Y%m%d").date()
                    current_day = datetime.strptime(trading_day, "%Y%m%d").date()
                except ValueError as exc:
                    activity_ready = False
                    activity_detail = f"invalid directional activity trading day: {exc}"
                else:
                    selected = (
                        select_contracts_from_activity(
                            config.directional,
                            catalog,
                            snapshot,
                            current_day,
                        )
                        if activity_day < current_day
                        else {}
                    )
                    configured_products = {
                        product.upper() for product in config.directional.products
                    }
                    selected_products = {product.upper() for product in selected}
                    missing_products = sorted(configured_products - selected_products)
                    activity_ready = bool(configured_products) and not missing_products
                    activity_detail = (
                        f"completed activity day: {snapshot.trading_day}; "
                        f"eligible products: {len(selected_products)}"
                        if activity_ready
                        else "activity/catalog coverage missing eligible products: "
                        + ", ".join(missing_products or sorted(configured_products))
                    )
    report.add("directional_activity_ready", activity_ready, activity_detail)

    cache_required_ready = True
    cache_required_detail = "directional strategy is disabled"
    if config.directional.enabled:
        if snapshot is None:
            cache_required_ready = False
            cache_required_detail = "completed directional activity day is unavailable"
        else:
            products = tuple(str(item).upper() for item in config.directional.products)
            cache_path = Path(config.state_path).with_name("directional_ohlc_cache.json")
            try:
                cache_entry = DirectionalOHLCCacheStore(cache_path).load(products)
            except (OSError, DirectionalOHLCCacheIntegrityError) as exc:
                cache_required_ready = False
                cache_required_detail = f"invalid directional OHLC cache: {exc}"
            else:
                required_day = snapshot.trading_date
                covered_dates = (
                    {pd_timestamp.date() for pd_timestamp in cache_entry.close.index}
                    if cache_entry
                    else set()
                )
                cache_required_ready = required_day in covered_dates
                cache_required_detail = (
                    f"directional OHLC cache covers required completed day "
                    f"{required_day.isoformat()}"
                    if cache_required_ready
                    else "directional OHLC cache does not cover required completed day "
                    f"{required_day.isoformat()}"
                )
                cache_facts = report.facts.get("directional_ohlc_cache")
                if isinstance(cache_facts, dict):
                    cache_facts["required_date"] = required_day.isoformat()
                    cache_facts["required_date_covered"] = cache_required_ready
    report.add(
        "directional_ohlc_cache_required_day",
        cache_required_ready,
        cache_required_detail,
    )

    broker_margin_ratio = account.margin / account.equity if account.equity > 0 else None
    report.facts["broker"] = {
        "ready": broker_ready,
        "fresh_snapshot": fresh_snapshot,
        "trading_day": trading_day,
        "account_equity": account.equity,
        "account_available": account.available,
        "account_margin": account.margin,
        "margin_ratio": broker_margin_ratio,
        "position_count": len([item for item in positions if not item.empty]),
        "active_order_count": active_order_count,
        "contract_catalog_count": len(catalog),
        "metadata_symbols": sorted(metadata),
        "orders_sent": 0,
        "cancels_sent": 0,
    }
    _add_stress90_doctor_status(
        report,
        config,
        trading_day=trading_day,
        account=account,
        positions=positions,
        active_order_count=active_order_count,
        catalog=catalog,
        requested_symbols=requested_symbols,
        metadata=metadata,
        quotes=dict(quotes or {}),
        session_trade_ownership_valid=session_trade_ownership_valid,
        session_trade_ownership_detail=session_trade_ownership_detail,
    )
    return report
