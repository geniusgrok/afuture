from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.stress90_operator_continuity import (
    Stress90OperatorContinuityError,
    Stress90OperatorContinuityEvidence,
    Stress90OperatorContinuityStore,
)

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_E = "e" * 64
_F = "f" * 64
_ONE = "1" * 64
_TWO = "2" * 64
_THREE = "3" * 64
_FOUR = "4" * 64


def _record(tmp_path: Path):
    runtime = tmp_path.resolve()
    evidence = Stress90OperatorContinuityEvidence(
        operation_id=_A,
        operator_reason="exclusive account continuity",
        confirmation_type="exclusive_account_no_external_activity",
        source_ctp_trading_day="20260828",
        target_ctp_trading_day="20260831",
        natural_day_gap=3,
        source_generic_state_sequence=7,
        source_generic_state_checksum=_B,
        source_policy_state_sequence=11,
        source_policy_state_checksum=_C,
        source_trading_day_evidence_sequence=5,
        source_trading_day_evidence_checksum=_D,
        account_identity_digest=_E,
        account_epoch=_F,
        canonical_runtime=str(runtime),
        canonical_runtime_digest=sha256(f"runtime:{runtime}".encode()).hexdigest(),
        source_account_registry_receipt_digest=_ONE,
        fresh_account_snapshot_digest=_TWO,
        position_reconciliation_digest=_THREE,
        session_ownership_digest=_FOUR,
        ctp_order_journal_digest=_B,
        active_order_count=0,
        deposit=0.0,
        withdrawal=0.0,
        source_last_account_equity=1_000_000.0,
        source_day_start_equity=990_000.0,
        source_hwm_equity=1_050_000.0,
        source_settlement_id="41",
        target_previous_settlement_equity=1_010_000.0,
        target_settlement_id="42",
        ohlc_latest_completed_day="20260828",
        activity_latest_completed_day="20260828",
        oi_latest_completed_day="20260828",
        policy_latest_completed_day="20260828",
        no_manual_trade=True,
        no_external_order=True,
        no_deposit=True,
        no_withdrawal=True,
        evidence_authority="operator_trust",
        authoritative_broker_or_exchange_evidence=False,
    )
    return Stress90OperatorContinuityStore(tmp_path / "stress90_operator_continuity.json").save(
        evidence,
        target_registry_receipt_digest=_C,
        target_trading_day_evidence_sequence=6,
        target_trading_day_evidence_checksum=_D,
    )


def _registry(
    record, *, operation_id: str = _A, operation_kind: str = "operator_managed_continuity"
):
    evidence = record.evidence
    return SimpleNamespace(
        binding_receipt_digest=record.target_registry_receipt_digest,
        binding=SimpleNamespace(
            account_identity_digest=evidence.account_identity_digest,
            account_epoch=evidence.account_epoch,
            canonical_runtime=evidence.canonical_runtime,
            runtime_identity_digest=evidence.canonical_runtime_digest,
            last_operation_id=operation_id,
            operation_kinds=(operation_kind,),
        ),
    )


def _tde(record, *, checksum: str | None = None):
    evidence = record.evidence
    return SimpleNamespace(
        sequence=record.target_trading_day_evidence_sequence,
        checksum=record.target_trading_day_evidence_checksum if checksum is None else checksum,
        trading_day=evidence.target_ctp_trading_day,
        account_identity_digest=evidence.account_identity_digest,
        account_epoch=evidence.account_epoch,
        canonical_runtime=evidence.canonical_runtime,
        runtime_identity_digest=evidence.canonical_runtime_digest,
        account_binding_receipt_digest=record.target_registry_receipt_digest,
        account_binding_last_operation_id=evidence.operation_id,
    )


def test_receipt_authority_requires_registry_operation_id_and_kind(tmp_path: Path):
    from afuture.stress90_operator_continuity import (
        require_stress90_operator_continuity_authority,
    )

    record = _record(tmp_path)
    with pytest.raises(
        Stress90OperatorContinuityError, match="registry.*operation|operation.*registry"
    ):
        require_stress90_operator_continuity_authority(
            record,
            registry_evidence=_registry(record, operation_id=_B),
            trading_day_evidence=_tde(record),
        )
    with pytest.raises(
        Stress90OperatorContinuityError, match="registry.*operation|operation.*registry"
    ):
        require_stress90_operator_continuity_authority(
            record,
            registry_evidence=_registry(record, operation_kind="account_rebase"),
            trading_day_evidence=_tde(record),
        )


def test_receipt_authority_requires_current_target_tde(tmp_path: Path):
    from afuture.stress90_operator_continuity import (
        require_stress90_operator_continuity_authority,
    )

    record = _record(tmp_path)
    with pytest.raises(Stress90OperatorContinuityError, match="TradingDayEvidence|TDE"):
        require_stress90_operator_continuity_authority(
            record,
            registry_evidence=_registry(record),
            trading_day_evidence=_tde(record, checksum=_E),
        )


def test_receipt_authority_accepts_exact_registry_and_tde_binding(tmp_path: Path):
    from afuture.stress90_operator_continuity import (
        require_stress90_operator_continuity_authority,
    )

    record = _record(tmp_path)
    assert (
        require_stress90_operator_continuity_authority(
            record,
            registry_evidence=_registry(record),
            trading_day_evidence=_tde(record),
        )
        == record
    )
