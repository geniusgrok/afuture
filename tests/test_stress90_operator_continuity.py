from __future__ import annotations

import json
import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from afuture.account_runtime_registry import (
    ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
    AccountRuntimeRegistry,
    AccountRuntimeRegistryError,
)
from afuture.directional import DirectionalConfig
from afuture.stress90_operator_continuity import (
    STRESS90_OPERATOR_CONTINUITY_CONFIRMATION,
    Stress90OperatorContinuityError,
    Stress90OperatorContinuityEvidence,
    Stress90OperatorContinuityStore,
    stress90_operator_continuity_request_digest,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64
_SHA_F = "f" * 64
_SHA_1 = "1" * 64
_SHA_2 = "2" * 64
_SHA_3 = "3" * 64
_SHA_4 = "4" * 64


def _evidence(runtime: Path, **changes: object) -> Stress90OperatorContinuityEvidence:
    base = Stress90OperatorContinuityEvidence(
        operation_id=_SHA_A,
        operator_reason="exclusive account rollover",
        confirmation_type="exclusive_account_no_external_activity",
        source_ctp_trading_day="20260828",
        target_ctp_trading_day="20260831",
        natural_day_gap=3,
        source_generic_state_sequence=7,
        source_generic_state_checksum=_SHA_B,
        source_policy_state_sequence=11,
        source_policy_state_checksum=_SHA_C,
        source_trading_day_evidence_sequence=5,
        source_trading_day_evidence_checksum=_SHA_D,
        account_identity_digest=_SHA_E,
        account_epoch=_SHA_F,
        canonical_runtime=str(runtime.resolve()),
        canonical_runtime_digest=sha256(
            f"runtime:{runtime.resolve()}".encode()
        ).hexdigest(),
        source_account_registry_receipt_digest=_SHA_2,
        fresh_account_snapshot_digest=_SHA_3,
        position_reconciliation_digest=_SHA_4,
        session_ownership_digest=_SHA_A,
        ctp_order_journal_digest=_SHA_B,
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


def test_continuity_mode_defaults_to_strict_and_validates_enum():
    assert DirectionalConfig().account_continuity_mode == "strict"
    DirectionalConfig(account_continuity_mode="strict").validate()
    with pytest.raises(ValueError, match="account_continuity_mode"):
        DirectionalConfig(account_continuity_mode="operator").validate()


def test_operator_evidence_accepts_explicit_weekend_gap_but_not_same_or_backward_day(tmp_path: Path):
    evidence = _evidence(tmp_path)
    digest = stress90_operator_continuity_request_digest(evidence)
    assert len(digest) == 64
    assert evidence.natural_day_gap == 3
    for target in ("20260828", "20260827"):
        with pytest.raises(Stress90OperatorContinuityError, match="strictly later"):
            stress90_operator_continuity_request_digest(
                replace(evidence, target_ctp_trading_day=target, natural_day_gap=0)
            )


def test_operator_evidence_requires_zero_cash_flow_no_orders_and_all_assertions(tmp_path: Path):
    evidence = _evidence(tmp_path)
    with pytest.raises(Stress90OperatorContinuityError, match="Deposit/Withdraw"):
        stress90_operator_continuity_request_digest(replace(evidence, deposit=1.0))
    with pytest.raises(Stress90OperatorContinuityError, match="active orders"):
        stress90_operator_continuity_request_digest(replace(evidence, active_order_count=1))
    with pytest.raises(Stress90OperatorContinuityError, match="operator assertions"):
        stress90_operator_continuity_request_digest(replace(evidence, no_manual_trade=False))
    with pytest.raises(Stress90OperatorContinuityError, match="operator_trust"):
        stress90_operator_continuity_request_digest(
            replace(evidence, evidence_authority="broker")
        )
    with pytest.raises(Stress90OperatorContinuityError, match="authoritative"):
        stress90_operator_continuity_request_digest(
            replace(evidence, authoritative_broker_or_exchange_evidence=True)
        )


def test_operator_evidence_requires_source_policy_and_data_alignment(tmp_path: Path):
    evidence = _evidence(tmp_path)
    for field in (
        "ohlc_latest_completed_day",
        "activity_latest_completed_day",
        "oi_latest_completed_day",
        "policy_latest_completed_day",
    ):
        with pytest.raises(Stress90OperatorContinuityError, match="source day"):
            stress90_operator_continuity_request_digest(
                replace(evidence, **{field: "20260827"})
            )


def test_store_chains_current_prev_and_exact_retry_is_idempotent(tmp_path: Path):
    path = tmp_path / "stress90_operator_continuity.json"
    store = Stress90OperatorContinuityStore(path)
    first = _evidence(tmp_path)
    record1 = store.save(
        first,
        target_registry_receipt_digest=_SHA_C,
        target_trading_day_evidence_sequence=6,
        target_trading_day_evidence_checksum=_SHA_D,
    )
    retry = store.save(
        first,
        target_registry_receipt_digest=_SHA_C,
        target_trading_day_evidence_sequence=6,
        target_trading_day_evidence_checksum=_SHA_D,
    )
    assert retry == record1
    assert record1.sequence == 1
    assert store.previous_path.exists() is False

    second = _evidence(
        tmp_path,
        operation_id=_SHA_B,
        source_ctp_trading_day="20260831",
        target_ctp_trading_day="20260901",
        natural_day_gap=1,
        source_generic_state_sequence=8,
        source_generic_state_checksum=_SHA_C,
        source_policy_state_sequence=12,
        source_policy_state_checksum=_SHA_D,
        source_trading_day_evidence_sequence=6,
        source_trading_day_evidence_checksum=_SHA_D,
        source_account_registry_receipt_digest=_SHA_C,
        ohlc_latest_completed_day="20260831",
        activity_latest_completed_day="20260831",
        oi_latest_completed_day="20260831",
        policy_latest_completed_day="20260831",
    )
    record2 = store.save(
        second,
        target_registry_receipt_digest=_SHA_E,
        target_trading_day_evidence_sequence=7,
        target_trading_day_evidence_checksum=_SHA_F,
    )
    assert record2.sequence == 2
    assert record2.parent_checksum == record1.checksum
    assert store.load_required_record() == record2
    assert store.load_previous_record() == record1


def test_store_rejects_operation_reuse_with_different_request(tmp_path: Path):
    store = Stress90OperatorContinuityStore(tmp_path / "stress90_operator_continuity.json")
    evidence = _evidence(tmp_path)
    store.save(
        evidence,
        target_registry_receipt_digest=_SHA_C,
        target_trading_day_evidence_sequence=6,
        target_trading_day_evidence_checksum=_SHA_D,
    )
    with pytest.raises(Stress90OperatorContinuityError, match="different request"):
        store.save(
            replace(evidence, operator_reason="different reason"),
            target_registry_receipt_digest=_SHA_C,
            target_trading_day_evidence_sequence=6,
            target_trading_day_evidence_checksum=_SHA_D,
        )


def test_store_never_falls_back_to_prev_or_deleted_current(tmp_path: Path):
    path = tmp_path / "stress90_operator_continuity.json"
    store = Stress90OperatorContinuityStore(path)
    first = _evidence(tmp_path)
    store.save(
        first,
        target_registry_receipt_digest=_SHA_C,
        target_trading_day_evidence_sequence=6,
        target_trading_day_evidence_checksum=_SHA_D,
    )
    second = _evidence(
        tmp_path,
        operation_id=_SHA_B,
        source_ctp_trading_day="20260831",
        target_ctp_trading_day="20260901",
        natural_day_gap=1,
        source_generic_state_sequence=8,
        source_generic_state_checksum=_SHA_C,
        source_policy_state_sequence=12,
        source_policy_state_checksum=_SHA_D,
        source_trading_day_evidence_sequence=6,
        source_trading_day_evidence_checksum=_SHA_D,
        source_account_registry_receipt_digest=_SHA_C,
        ohlc_latest_completed_day="20260831",
        activity_latest_completed_day="20260831",
        oi_latest_completed_day="20260831",
        policy_latest_completed_day="20260831",
    )
    store.save(
        second,
        target_registry_receipt_digest=_SHA_E,
        target_trading_day_evidence_sequence=7,
        target_trading_day_evidence_checksum=_SHA_F,
    )
    path.unlink()
    with pytest.raises(Stress90OperatorContinuityError, match="current is missing"):
        store.load_record()


def test_store_rejects_symlink_duplicate_key_invalid_utf8_and_truncation(tmp_path: Path):
    path = tmp_path / "stress90_operator_continuity.json"
    store = Stress90OperatorContinuityStore(path)
    evidence = _evidence(tmp_path)
    store.save(
        evidence,
        target_registry_receipt_digest=_SHA_C,
        target_trading_day_evidence_sequence=6,
        target_trading_day_evidence_checksum=_SHA_D,
    )
    good = path.read_bytes()

    real = tmp_path / "real.json"
    real.write_bytes(good)
    path.unlink()
    path.symlink_to(real)
    with pytest.raises(Stress90OperatorContinuityError, match="regular file|symlink"):
        store.load_record()
    path.unlink()

    text = good.decode("utf-8")
    path.write_text(text.replace('"kind":', '"kind": "duplicate", "kind":', 1), encoding="utf-8")
    with pytest.raises(Stress90OperatorContinuityError, match="duplicate"):
        store.load_record()

    path.write_bytes(b"\xff\xfe")
    with pytest.raises(Stress90OperatorContinuityError, match="UTF-8"):
        store.load_record()

    path.write_bytes(good[: max(1, len(good) // 2)])
    with pytest.raises(Stress90OperatorContinuityError, match="JSON"):
        store.load_record()


def test_store_rejects_nan_and_infinity_even_with_recomputed_text_untrusted(tmp_path: Path):
    path = tmp_path / "stress90_operator_continuity.json"
    store = Stress90OperatorContinuityStore(path)
    evidence = _evidence(tmp_path)
    store.save(
        evidence,
        target_registry_receipt_digest=_SHA_C,
        target_trading_day_evidence_sequence=6,
        target_trading_day_evidence_checksum=_SHA_D,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["evidence"]["deposit"] = float("nan")
    path.write_text(json.dumps(raw, allow_nan=True), encoding="utf-8")
    with pytest.raises(Stress90OperatorContinuityError, match="finite|NaN|JSON"):
        store.load_record()


def test_artifact_is_bound_to_runtime_account_and_epoch(tmp_path: Path):
    record = Stress90OperatorContinuityStore(
        tmp_path / "stress90_operator_continuity.json"
    ).save(
        _evidence(tmp_path),
        target_registry_receipt_digest=_SHA_C,
        target_trading_day_evidence_sequence=6,
        target_trading_day_evidence_checksum=_SHA_D,
    )
    store = Stress90OperatorContinuityStore(tmp_path / "stress90_operator_continuity.json")
    store.require_current_binding(
        account_identity_digest=_SHA_E,
        account_epoch=_SHA_F,
        canonical_runtime=tmp_path,
        target_ctp_trading_day="20260831",
    )
    for account, epoch, runtime in (
        (_SHA_D, _SHA_F, tmp_path),
        (_SHA_E, _SHA_D, tmp_path),
        (_SHA_E, _SHA_F, tmp_path / "other"),
    ):
        with pytest.raises(Stress90OperatorContinuityError, match="binding"):
            store.require_current_binding(
                account_identity_digest=account,
                account_epoch=epoch,
                canonical_runtime=runtime,
                target_ctp_trading_day=record.evidence.target_ctp_trading_day,
            )


def test_registry_consumes_operator_continuity_nonce_with_semantic_digest(tmp_path: Path):
    registry = AccountRuntimeRegistry(tmp_path / "registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    registry.bind_new(_SHA_E, runtime, _SHA_F, _SHA_A)
    request = _SHA_B
    record = registry.acknowledge_operator_continuity_operation(
        _SHA_E,
        runtime,
        _SHA_F,
        _SHA_C,
        request,
    )
    binding = registry.require_binding(_SHA_E, runtime, _SHA_F)
    assert binding.operation_kinds[-1] == "operator_managed_continuity"
    assert record.sequence >= 3
    assert (
        registry.acknowledge_operator_continuity_operation(
            _SHA_E, runtime, _SHA_F, _SHA_C, request
        )
        == record
    )
    with pytest.raises(AccountRuntimeRegistryError, match="different request"):
        registry.acknowledge_operator_continuity_operation(
            _SHA_E, runtime, _SHA_F, _SHA_C, _SHA_D
        )


def test_confirmation_constant_is_deliberately_strong():
    assert (
        STRESS90_OPERATOR_CONTINUITY_CONFIRMATION
        == "I_CONFIRM_EXCLUSIVE_ACCOUNT_AND_NO_EXTERNAL_ACTIVITY"
    )
    assert os.environ.get("AFUTURE_OPERATOR_CONTINUITY_ACK", "") != "__weak_default__"
