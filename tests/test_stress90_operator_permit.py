from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest


def _base_evidence():
    from tests.test_stress90_activation_permit import _evidence

    return _evidence()


def test_strict_activation_evidence_remains_default_and_has_no_operator_receipt():
    evidence = _base_evidence()
    assert evidence.account_continuity_mode == "strict"
    assert evidence.operator_continuity_receipt_digest == ""


def test_operator_managed_activation_evidence_requires_receipt_digest(tmp_path: Path):
    from afuture.stress90_activation_permit import Stress90ActivationPermitStore

    store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    with pytest.raises(RuntimeError, match="operator continuity receipt"):
        store.issue(replace(_base_evidence(), account_continuity_mode="operator_managed"))

    issued = store.issue(
        replace(
            _base_evidence(),
            account_continuity_mode="operator_managed",
            operator_continuity_receipt_digest="a" * 64,
        )
    )
    assert issued.permit.external_activation_gates_completed is False


def test_operator_receipt_or_mode_change_invalidates_issued_permit(tmp_path: Path):
    from afuture.stress90_activation_permit import Stress90ActivationPermitStore

    operator = replace(
        _base_evidence(),
        account_continuity_mode="operator_managed",
        operator_continuity_receipt_digest="a" * 64,
    )
    store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    issued = store.issue(operator)

    with pytest.raises(RuntimeError, match="evidence mismatch"):
        store.consume(
            replace(operator, operator_continuity_receipt_digest="b" * 64),
            expected_sequence=issued.sequence,
        )
    with pytest.raises(RuntimeError, match="evidence mismatch"):
        store.consume(
            replace(
                operator,
                account_continuity_mode="strict",
                operator_continuity_receipt_digest="",
            ),
            expected_sequence=issued.sequence,
        )


def test_operator_receipt_never_completes_external_activation_gates(tmp_path: Path):
    from afuture.stress90_activation_permit import (
        STRESS90_ACTIVATION_PERMIT_SCOPE,
        Stress90ActivationPermitStore,
    )

    evidence = replace(
        _base_evidence(),
        account_continuity_mode="operator_managed",
        operator_continuity_receipt_digest="c" * 64,
    )
    record = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json").issue(
        evidence
    )
    assert record.permit.scope == STRESS90_ACTIVATION_PERMIT_SCOPE
    assert record.permit.external_activation_gates_completed is False
