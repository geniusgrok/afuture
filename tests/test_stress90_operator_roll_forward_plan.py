from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

_ACCOUNT = "a" * 64
_EPOCH = "b" * 64
_OPERATION = "c" * 64


def _plan_inputs(tmp_path: Path, *, deposit: float = 0.0, withdrawal: float = 0.0):
    from afuture.account_runtime_registry import (
        AccountRuntimeBinding,
        AccountRuntimeBindingEvidence,
    )
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90StateRecord,
        bind_stress90_account_identity,
    )
    from afuture.models import AccountSnapshot, RuntimeMode
    from afuture.state import RuntimeState, RuntimeStateRecord
    from afuture.stress90_operator_continuity import (
        Stress90OperatorAccountDayContinuityEvidence,
    )
    from afuture.trading_day_evidence import TradingDayEvidence

    runtime = tmp_path.resolve()
    runtime_digest = sha256(f"runtime:{runtime}".encode()).hexdigest()
    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260828",
        last_decision_digest="1" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "2" * 64},
        bootstrap_through_day="20260828",
        last_completed_input_day="20260827",
    )
    policy_state = bind_stress90_account_identity(
        Stress90PolicyState.from_seed(seed),
        _ACCOUNT,
        account_epoch=_EPOCH,
    )
    policy_state = replace(
        policy_state,
        live_inception_day="20260827",
        live_inception_equity=700_000.0,
    )
    policy = Stress90StateRecord(policy_state, 7, "3" * 64)
    generic_state = activate_stress90_policy(
        RuntimeState(
            kill_switch=True,
            kill_reason="operator continuity fixture",
            reconciled=True,
            metadata_verified=True,
            runtime_mode=RuntimeMode.HALTED.value,
            trading_day="20260828",
            day_start_equity=700_000.0,
            equity_high_watermark=720_000.0,
            last_account_equity=690_000.0,
            last_account_trading_day="20260828",
            last_account_deposit=0.0,
            last_account_withdrawal=0.0,
            last_account_cash_flow_verified=True,
            last_account_settlement_id=43,
        ),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=seed.seed_digest,
        account_identity_digest=_ACCOUNT,
        operator_reason="operator continuity fixture",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    generic = RuntimeStateRecord(generic_state, 11, "4" * 64, False)
    binding = AccountRuntimeBinding(
        account_identity_digest=_ACCOUNT,
        canonical_runtime=str(runtime),
        runtime_identity_digest=runtime_digest,
        account_epoch=_EPOCH,
        last_operation_id="5" * 64,
        operation_history=("5" * 64,),
        operation_kinds=("bind",),
        last_operation_receipt_digest="6" * 64,
        binding_revision=4,
        binding_payload_digest="7" * 64,
    )
    registry = AccountRuntimeBindingEvidence(
        registry_sequence=9,
        registry_checksum="8" * 64,
        binding=binding,
        binding_payload_digest="7" * 64,
        binding_revision=4,
        binding_receipt_digest="9" * 64,
        registry_nonce_root="d" * 64,
        registry_nonce_count=3,
    )
    tde = TradingDayEvidence(
        phase="bound",
        trading_day="20260828",
        account_identity_digest=_ACCOUNT,
        account_epoch=_EPOCH,
        canonical_runtime=str(runtime),
        runtime_identity_digest=runtime_digest,
        account_binding_payload_digest="7" * 64,
        account_binding_revision=4,
        account_binding_last_operation_id="5" * 64,
        account_binding_receipt_digest="9" * 64,
        registry_sequence=9,
        registry_checksum="8" * 64,
        rebind_transaction_id="5" * 64,
        previous_account_identity_digest=_ACCOUNT,
        sequence=13,
        checksum="e" * 64,
    )
    account = AccountSnapshot(
        balance=685_000.0,
        equity=685_000.0,
        available=685_000.0,
        margin=0.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        trading_day="20260831",
        deposit=deposit,
        withdrawal=withdrawal,
        cash_flow_verified=True,
        previous_settlement_equity=686_000.0,
        settlement_verified=True,
        settlement_id=44,
    )
    market = Stress90OperatorAccountDayContinuityEvidence(
        completed_account_day="20260828",
        current_ctp_trading_day="20260831",
        natural_day_gap=3,
        ohlc_content_digest="a" * 64,
        oi_store_checksum="b" * 64,
        completed_oi_evidence_digest="c" * 64,
        observed_transition_digest="d" * 64,
        continuity_digest="f" * 64,
    )
    return dict(
        operation_id=_OPERATION,
        operator_reason="exclusive account Friday-to-Monday continuity",
        generic_source=generic,
        policy_source=policy,
        source_trading_day_evidence=tde,
        source_registry_evidence=registry,
        runtime_dir=runtime,
        account_identity_digest=_ACCOUNT,
        account_snapshot=account,
        active_order_count=0,
        positions_reconciled=True,
        position_reconciliation_digest="1" * 64,
        session_ownership_digest="2" * 64,
        ctp_order_journal_digest="3" * 64,
        activity_latest_completed_day="20260828",
        market_continuity=market,
    )


def test_operator_roll_forward_plan_builds_existing_settlement_targets_and_keeps_halt(
    tmp_path: Path,
):
    from afuture.stress90_operator_continuity import build_stress90_operator_roll_forward_plan

    plan = build_stress90_operator_roll_forward_plan(**_plan_inputs(tmp_path))

    assert plan.targets.completed_account_day == "20260828"
    assert plan.targets.current_ctp_trading_day == "20260831"
    assert plan.targets.generic_target.trading_day == "20260831"
    assert plan.targets.generic_target.kill_switch is True
    assert plan.targets.generic_target.runtime_mode == "HALTED"
    assert plan.targets.generic_target.metadata_verified is False
    assert plan.targets.policy_target.last_completed_account_day == "20260828"
    assert plan.evidence.evidence_authority == "operator_trust"
    assert plan.evidence.authoritative_broker_or_exchange_evidence is False
    assert plan.evidence.natural_day_gap == 3
    assert plan.evidence.source_account_registry_receipt_digest == "9" * 64
    assert plan.request_digest


def test_operator_roll_forward_plan_rejects_cash_flow_and_requires_rebase(tmp_path: Path):
    from afuture.stress90_operator_continuity import (
        Stress90OperatorContinuityError,
        build_stress90_operator_roll_forward_plan,
    )

    with pytest.raises(Stress90OperatorContinuityError, match="Deposit|Withdraw|rebase|cash flow"):
        build_stress90_operator_roll_forward_plan(**_plan_inputs(tmp_path, deposit=1.0))


def test_operator_roll_forward_plan_rejects_position_drift(tmp_path: Path):
    from afuture.stress90_operator_continuity import (
        Stress90OperatorContinuityError,
        build_stress90_operator_roll_forward_plan,
    )

    values = _plan_inputs(tmp_path)
    values["positions_reconciled"] = False
    with pytest.raises(Stress90OperatorContinuityError, match="position reconciliation"):
        build_stress90_operator_roll_forward_plan(**values)


def test_operator_roll_forward_plan_rejects_source_policy_or_activity_day_mismatch(tmp_path: Path):
    from afuture.stress90_operator_continuity import (
        Stress90OperatorContinuityError,
        build_stress90_operator_roll_forward_plan,
    )

    values = _plan_inputs(tmp_path)
    values["activity_latest_completed_day"] = "20260827"
    with pytest.raises(Stress90OperatorContinuityError, match="activity|source"):
        build_stress90_operator_roll_forward_plan(**values)

    values = _plan_inputs(tmp_path)
    values["policy_source"] = replace(
        values["policy_source"],
        state=replace(values["policy_source"].state, last_completed_target_day="20260827"),
    )
    with pytest.raises(Stress90OperatorContinuityError, match="policy|source"):
        build_stress90_operator_roll_forward_plan(**values)
