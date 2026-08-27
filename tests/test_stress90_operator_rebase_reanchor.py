from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

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


def _evidence(runtime: Path, **changes: object) -> Stress90OperatorContinuityEvidence:
    canonical = runtime.resolve()
    base = Stress90OperatorContinuityEvidence(
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
        canonical_runtime=str(canonical),
        canonical_runtime_digest=sha256(f"runtime:{canonical}".encode()).hexdigest(),
        source_account_registry_receipt_digest=_TWO,
        fresh_account_snapshot_digest=_THREE,
        position_reconciliation_digest=_FOUR,
        session_ownership_digest=_A,
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
    return replace(base, **changes)


def test_same_account_rebase_can_reanchor_new_epoch_after_registry_and_tde_advance(tmp_path: Path):
    store = Stress90OperatorContinuityStore(tmp_path / "stress90_operator_continuity.json")
    first = store.save(
        _evidence(tmp_path),
        target_registry_receipt_digest=_C,
        target_trading_day_evidence_sequence=6,
        target_trading_day_evidence_checksum=_D,
    )

    after_rebase = _evidence(
        tmp_path,
        operation_id=_B,
        source_ctp_trading_day="20260831",
        target_ctp_trading_day="20260901",
        natural_day_gap=1,
        source_generic_state_sequence=9,
        source_generic_state_checksum=_D,
        source_policy_state_sequence=13,
        source_policy_state_checksum=_E,
        source_trading_day_evidence_sequence=7,
        source_trading_day_evidence_checksum=_E,
        account_epoch=_ONE,
        source_account_registry_receipt_digest=_F,
        ohlc_latest_completed_day="20260831",
        activity_latest_completed_day="20260831",
        oi_latest_completed_day="20260831",
        policy_latest_completed_day="20260831",
    )
    second = store.save(
        after_rebase,
        target_registry_receipt_digest=_TWO,
        target_trading_day_evidence_sequence=8,
        target_trading_day_evidence_checksum=_THREE,
    )

    assert second.sequence == first.sequence + 1
    assert second.parent_checksum == first.checksum
    assert second.evidence.account_epoch == _ONE
    assert second.evidence.account_identity_digest == first.evidence.account_identity_digest
    assert second.evidence.canonical_runtime == first.evidence.canonical_runtime


def test_rebase_reanchor_still_rejects_account_or_runtime_reuse(tmp_path: Path):
    store = Stress90OperatorContinuityStore(tmp_path / "stress90_operator_continuity.json")
    store.save(
        _evidence(tmp_path),
        target_registry_receipt_digest=_C,
        target_trading_day_evidence_sequence=6,
        target_trading_day_evidence_checksum=_D,
    )
    base = _evidence(
        tmp_path,
        operation_id=_B,
        source_ctp_trading_day="20260831",
        target_ctp_trading_day="20260901",
        natural_day_gap=1,
        source_generic_state_sequence=9,
        source_generic_state_checksum=_D,
        source_policy_state_sequence=13,
        source_policy_state_checksum=_E,
        source_trading_day_evidence_sequence=7,
        source_trading_day_evidence_checksum=_E,
        account_epoch=_ONE,
        source_account_registry_receipt_digest=_F,
        ohlc_latest_completed_day="20260831",
        activity_latest_completed_day="20260831",
        oi_latest_completed_day="20260831",
        policy_latest_completed_day="20260831",
    )
    with pytest.raises(Stress90OperatorContinuityError, match="account|runtime|chain"):
        store.save(
            replace(base, account_identity_digest=_FOUR),
            target_registry_receipt_digest=_TWO,
            target_trading_day_evidence_sequence=8,
            target_trading_day_evidence_checksum=_THREE,
        )

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(Stress90OperatorContinuityError, match="account|runtime|chain"):
        store.save(
            replace(
                base,
                canonical_runtime=str(other.resolve()),
                canonical_runtime_digest=sha256(f"runtime:{other.resolve()}".encode()).hexdigest(),
            ),
            target_registry_receipt_digest=_TWO,
            target_trading_day_evidence_sequence=8,
            target_trading_day_evidence_checksum=_THREE,
        )
