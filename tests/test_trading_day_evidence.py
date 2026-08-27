from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _initialized_registry(path: Path):
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )

    registry = AccountRuntimeRegistry(path)
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    return registry


def _policy(account: str | None, epoch: str | None):
    return SimpleNamespace(
        live_account_identity_digest=account,
        live_account_epoch=epoch,
    )


def _transaction(*, account: str, epoch: str, nonce: str, source_account: str = ""):
    return SimpleNamespace(
        operation="activation" if not source_account else "account_rebase",
        status="prepared",
        transaction_id="d" * 64,
        operation_nonce=nonce,
        source_account_identity_digest=source_account,
        source_account_epoch="" if not source_account else "c" * 64,
        account_identity_digest=account,
        trading_day="20260825",
        policy_target=_policy(account, epoch),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_unbound_evidence_has_an_explicit_phase_and_never_invents_registry_lineage(
    tmp_path: Path,
) -> None:
    from afuture.trading_day_evidence import (
        TradingDayEvidenceStore,
        require_authoritative_trading_day_evidence,
    )

    runtime = tmp_path / "runtime"
    registry = _initialized_registry(tmp_path / "registry.json")
    store = TradingDayEvidenceStore(runtime / "ctp_trading_day_evidence.json")
    evidence = store.save_observation(
        trading_day="20260825",
        account_identity_digest="a" * 64,
        runtime_dir=runtime,
        policy_state=_policy(None, None),
        registry=registry,
    )

    assert evidence.phase == "unbound"
    assert evidence.account_epoch == ""
    assert evidence.account_binding_revision == 0
    assert evidence.account_binding_payload_digest == ""
    assert evidence.account_binding_last_operation_id == ""
    assert evidence.account_binding_receipt_digest == ""
    assert evidence.registry_sequence == 0
    assert evidence.registry_checksum == ""
    require_authoritative_trading_day_evidence(
        evidence,
        policy_state=_policy(None, None),
        registry=registry,
        runtime_dir=runtime,
        lifecycle_transaction=None,
    )

    registry.bind_new("a" * 64, runtime, "b" * 64, "c" * 64)
    with pytest.raises(RuntimeError, match="unbound.*active binding"):
        require_authoritative_trading_day_evidence(
            evidence,
            policy_state=_policy(None, None),
            registry=registry,
            runtime_dir=runtime,
            lifecycle_transaction=None,
        )


def test_lifecycle_evidence_binds_exact_policy_epoch_and_stable_account_receipt(
    tmp_path: Path,
) -> None:
    from afuture.trading_day_evidence import (
        TradingDayEvidenceStore,
        require_authoritative_trading_day_evidence,
    )

    runtime = tmp_path / "runtime"
    registry = _initialized_registry(tmp_path / "registry.json")
    store = TradingDayEvidenceStore(runtime / "ctp_trading_day_evidence.json")
    store.save_observation(
        trading_day="20260825",
        account_identity_digest="a" * 64,
        runtime_dir=runtime,
        policy_state=_policy(None, None),
        registry=registry,
    )
    target_epoch = "e" * 64
    operation_nonce = "f" * 64
    transaction = _transaction(
        account="a" * 64,
        epoch=target_epoch,
        nonce=operation_nonce,
    )
    registry.bind_new("a" * 64, runtime, target_epoch, operation_nonce)
    receipt = registry.require_binding_evidence("a" * 64, runtime, target_epoch)

    bound = store.bind_for_lifecycle(
        transaction=transaction,
        runtime_dir=runtime,
        binding_evidence=receipt,
    )
    exact_retry = store.bind_for_lifecycle(
        transaction=transaction,
        runtime_dir=runtime,
        binding_evidence=receipt,
    )

    assert exact_retry == bound
    assert bound.phase == "bound"
    assert bound.account_epoch == target_epoch
    assert bound.account_epoch != operation_nonce
    assert bound.account_binding_payload_digest == receipt.binding_payload_digest
    assert bound.account_binding_revision == receipt.binding_revision
    assert bound.account_binding_last_operation_id == operation_nonce
    assert bound.account_binding_receipt_digest == receipt.binding_receipt_digest
    require_authoritative_trading_day_evidence(
        bound,
        policy_state=_policy("a" * 64, target_epoch),
        registry=registry,
        runtime_dir=runtime,
        lifecycle_transaction=None,
    )

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    wrong_runtime = str((tmp_path / "wrong-runtime").resolve())
    raw["canonical_runtime"] = wrong_runtime
    raw["runtime_identity_digest"] = hashlib.sha256(f"runtime:{wrong_runtime}".encode()).hexdigest()
    raw["checksum"] = _digest({key: value for key, value in raw.items() if key != "checksum"})
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RuntimeError, match="source|runtime"):
        store.bind_for_lifecycle(
            transaction=transaction,
            runtime_dir=runtime,
            binding_evidence=receipt,
        )

    registry.bind_new("1" * 64, tmp_path / "other-runtime", "2" * 64, "3" * 64)
    require_authoritative_trading_day_evidence(
        bound,
        policy_state=_policy("a" * 64, target_epoch),
        registry=registry,
        runtime_dir=runtime,
        lifecycle_transaction=None,
    )


def test_nonce_as_epoch_is_rejected_even_with_a_valid_record_checksum(tmp_path: Path) -> None:
    from afuture.trading_day_evidence import (
        TradingDayEvidenceError,
        TradingDayEvidenceStore,
        require_authoritative_trading_day_evidence,
    )

    runtime = tmp_path / "runtime"
    registry = _initialized_registry(tmp_path / "registry.json")
    target_epoch = "e" * 64
    operation_nonce = "f" * 64
    registry.bind_new("a" * 64, runtime, target_epoch, operation_nonce)
    store = TradingDayEvidenceStore(runtime / "ctp_trading_day_evidence.json")
    store.bind_for_lifecycle(
        transaction=_transaction(
            account="a" * 64,
            epoch=target_epoch,
            nonce=operation_nonce,
        ),
        runtime_dir=runtime,
        binding_evidence=registry.require_binding_evidence("a" * 64, runtime, target_epoch),
    )
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["account_epoch"] = operation_nonce
    unsigned = {key: value for key, value in raw.items() if key != "checksum"}
    raw["checksum"] = _digest(unsigned)
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises((TradingDayEvidenceError, RuntimeError), match="epoch"):
        require_authoritative_trading_day_evidence(
            store.load_required(),
            policy_state=_policy("a" * 64, target_epoch),
            registry=registry,
            runtime_dir=runtime,
            lifecycle_transaction=None,
        )


def test_prepared_lifecycle_blocks_evidence_authority(tmp_path: Path) -> None:
    from afuture.trading_day_evidence import (
        TradingDayEvidenceStore,
        require_authoritative_trading_day_evidence,
    )

    runtime = tmp_path / "runtime"
    registry = _initialized_registry(tmp_path / "registry.json")
    evidence = TradingDayEvidenceStore(runtime / "ctp_trading_day_evidence.json").save_observation(
        trading_day="20260825",
        account_identity_digest="a" * 64,
        runtime_dir=runtime,
        policy_state=_policy(None, None),
        registry=registry,
    )

    with pytest.raises(RuntimeError, match="lifecycle.*prepared"):
        require_authoritative_trading_day_evidence(
            evidence,
            policy_state=_policy(None, None),
            registry=registry,
            runtime_dir=runtime,
            lifecycle_transaction=SimpleNamespace(status="prepared"),
        )


def test_old_schema_cannot_be_observed_or_inferred_without_exact_lifecycle(
    tmp_path: Path,
) -> None:
    from afuture.trading_day_evidence import TradingDayEvidenceError, TradingDayEvidenceStore

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    registry = _initialized_registry(tmp_path / "registry.json")
    store = TradingDayEvidenceStore(runtime / "ctp_trading_day_evidence.json")
    unsigned = {
        "kind": "afuture.ctp.trading-day-evidence",
        "schema_version": 2,
        "sequence": 1,
        "trading_day": "20260825",
        "account_identity_digest": "a" * 64,
        "account_epoch": "a" * 64,
        "rebind_transaction_id": "",
        "previous_account_identity_digest": "",
    }
    store.path.write_text(
        json.dumps({**unsigned, "checksum": _digest(unsigned)}),
        encoding="utf-8",
    )

    with pytest.raises(TradingDayEvidenceError, match="schema"):
        store.load_required()
    with pytest.raises(TradingDayEvidenceError, match="old.*exact lifecycle"):
        store.save_observation(
            trading_day="20260825",
            account_identity_digest="a" * 64,
            runtime_dir=runtime,
            policy_state=_policy(None, None),
            registry=registry,
        )

    epoch = "e" * 64
    nonce = "f" * 64
    transaction = _transaction(account="a" * 64, epoch=epoch, nonce=nonce)
    registry.bind_new("a" * 64, runtime, epoch, nonce)
    upgraded = store.bind_for_lifecycle(
        transaction=transaction,
        runtime_dir=runtime,
        binding_evidence=registry.require_binding_evidence("a" * 64, runtime, epoch),
    )
    assert upgraded.phase == "bound"
    assert upgraded.account_epoch == epoch
