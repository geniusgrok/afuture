from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Barrier, Event, Thread, get_ident
from time import monotonic, sleep
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

_CHINA = ZoneInfo("Asia/Shanghai")
_ACCOUNT_IDENTITY = "a" * 64
_ACCOUNT_EPOCH = "b" * 64


def _write_seed_state(tmp_path: Path, *, last_target_day: str = "20260824"):
    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        Stress90SeedStore,
    )

    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day=last_target_day,
        last_decision_digest="a" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "1" * 64},
        bootstrap_through_day=last_target_day,
        last_completed_input_day="20260823",
    )
    seed_path = tmp_path / "stress90_bootstrap_seed.json"
    state_path = tmp_path / "stress90_policy_state.json"
    Stress90SeedStore(seed_path).save_new(seed)
    Stress90PolicyStateStore(state_path).save(Stress90PolicyState.from_seed(seed))
    return seed_path, state_path


def _bind_seed_state_to_test_account(state_path: Path) -> None:
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        bind_stress90_account_identity,
    )

    store = Stress90PolicyStateStore(state_path)
    record = store.load_required_record()
    store.save(
        bind_stress90_account_identity(
            record.state,
            _ACCOUNT_IDENTITY,
            account_epoch=_ACCOUNT_EPOCH,
        ),
        expected_sequence=record.sequence,
    )


def _write_completed_oi(tmp_path: Path):
    from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
    )
    from afuture.models import ContractInfo, Tick

    products = ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")
    catalog = [
        ContractInfo(
            f"{product}2612",
            PRODUCT_SESSION_MANIFEST[product].exchange,
            product,
            "2026-12-15",
        )
        for product in products
    ]
    path = tmp_path / "stress90_oi_evidence.json"
    aggregator = Stress90OiEvidenceAggregator(store=Stress90OiEvidenceStore(path))
    aggregator.set_expected_contracts("20260824", catalog)
    aggregator.note_raw_market_connection(connected=True, generation=1)
    for contract in catalog:
        for timestamp, price, volume, hold in (
            (datetime(2026, 8, 23, 21, 0, tzinfo=_CHINA), 100.0, 10.0, 100.0),
            (datetime(2026, 8, 24, 14, 59, tzinfo=_CHINA), 101.0, 20.0, 110.0),
        ):
            aggregator.observe_raw_tick(
                Tick(
                    contract.symbol,
                    contract.exchange,
                    timestamp,
                    price - 0.5,
                    price + 0.5,
                    price,
                    100.0,
                    100.0,
                    "20260824",
                    volume=volume,
                    open_interest=hold,
                    open_price=100.0,
                ),
                contract,
            )
    aggregator.set_expected_contracts("20260825", catalog)
    aggregator.observe_raw_tick(
        Tick(
            catalog[0].symbol,
            catalog[0].exchange,
            datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
            100.5,
            101.5,
            101.0,
            100.0,
            100.0,
            "20260825",
            volume=1.0,
            open_interest=110.0,
            open_price=101.0,
        ),
        catalog[0],
    )
    aggregator.checkpoint()
    return path


def test_execution_intent_is_persisted_once_and_reused_after_broker_positions_change(
    tmp_path: Path,
):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    store = Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json")
    first = prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={"A2612": 2, "AG2612": 2},
        margin_fitted_lots={"A2612": -10, "AG2702": 3},
        symbol_products={"A2612": "A", "AG2612": "AG", "AG2702": "AG"},
        lot_notionals={"A2612": 10_000.0, "AG2612": 20_000.0},
    )
    restarted = prepare_stress90_execution_intent(
        Stress90ExecutionIntentStore(store.path),
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={"A2612": -10, "AG2702": 3},
        symbol_products={"A2612": "A", "AG2702": "AG"},
        lot_notionals={"A2612": 10_000.0, "AG2702": 20_000.0},
    )

    assert first.authorized_transition_products == ("A", "AG")
    assert restarted == first
    assert store.load_required_record().sequence == 1


def test_execution_intent_persists_typed_roll_and_freeze_authorized_target(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    store = Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json")
    intent = prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={"A2612": 5},
        margin_fitted_lots={"A2701": 10},
        freeze_authorized_lots={"A2701": 2},
        symbol_products={"A2612": "A", "A2701": "A"},
        lot_notionals={"A2612": 10_000.0, "A2701": 20_000.0},
    )

    assert intent.freeze_authorized_lots == {"A2701": 2}
    assert len(intent.transitions) == 1
    transition = intent.transitions[0]
    assert transition.product == "A"
    assert transition.kind == "same_product_roll"
    assert transition.source_symbols == ("A2612",)
    assert transition.target_symbol == "A2701"
    assert transition.target_sign == 1
    assert transition.max_replacement_notional == 50_000.0
    assert intent.authorized_transition_products == ("A",)
    assert dict(getattr(intent, "authorized_transition_max_replacement_notionals", {})) == {
        "A": 50_000.0
    }

    restarted = prepare_stress90_execution_intent(
        Stress90ExecutionIntentStore(store.path),
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={"A2701": 10},
        freeze_authorized_lots={"A2701": 10},
        symbol_products={"A2701": "A"},
        lot_notionals={"A2701": 20_000.0},
    )
    assert restarted == intent


@pytest.mark.parametrize("mutation", ["old_schema", "missing_overlay"])
def test_execution_intent_rejects_non_current_evidence_without_rewriting(
    tmp_path: Path,
    mutation: str,
) -> None:
    from afuture import directional_stress90_execution as execution_module
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    store = Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json")
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        risk_overlay_digest="c" * 64,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    if mutation == "old_schema":
        raw["schema_version"] = 6
    else:
        intent = raw["intent"]
        intent.pop("risk_overlay_digest")
        intent["source_digest"] = execution_module._digest(
            {
                "account_epoch": intent["account_epoch"],
                "account_identity_digest": intent["account_identity_digest"],
                "current_lots": intent["initial_current_lots"],
                "decision_digest": intent["daily_decision_digest"],
                "freeze_authorized_lots": intent["freeze_authorized_lots"],
                "margin_fitted_lots": intent["initial_margin_fitted_lots"],
                "target_trading_day": intent["target_trading_day"],
                "transitions": intent["transitions"],
            }
        )
    raw["checksum"] = execution_module._digest(
        {key: value for key, value in raw.items() if key != "checksum"}
    )
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    original = store.path.read_bytes()

    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="schema|fields"):
        store.load_required_record()

    assert store.path.read_bytes() == original


def test_execution_intent_refuses_to_fabricate_roll_replacement_notional(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="lot notional"):
        prepare_stress90_execution_intent(
            Stress90ExecutionIntentStore(tmp_path / "intent.json"),
            target_trading_day="20260825",
            daily_decision_digest="1" * 64,
            account_identity_digest=_ACCOUNT_IDENTITY,
            account_epoch=_ACCOUNT_EPOCH,
            current_lots={"A2612": 5},
            margin_fitted_lots={"A2701": 10},
            freeze_authorized_lots={"A2701": 2},
            symbol_products={"A2612": "A", "A2701": "A"},
            lot_notionals={},
        )


def test_execution_intent_rejects_identity_change_and_never_uses_corrupt_prev(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    path = tmp_path / "stress90_execution_intent.json"
    store = Stress90ExecutionIntentStore(path)
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="decision identity"):
        prepare_stress90_execution_intent(
            store,
            target_trading_day="20260825",
            daily_decision_digest="2" * 64,
            account_identity_digest=_ACCOUNT_IDENTITY,
            account_epoch=_ACCOUNT_EPOCH,
            current_lots={},
            margin_fitted_lots={},
            symbol_products={},
        )
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260826",
        daily_decision_digest="3" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    assert store.previous_path.exists()
    path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="JSON"):
        Stress90ExecutionIntentStore(path).load_required_record()


@pytest.mark.parametrize("sequence", [1, 2])
def test_execution_intent_accepts_exact_current_prev_duplicate_after_atomic_replace_crash(
    tmp_path: Path,
    sequence: int,
) -> None:
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    store = Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json")
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    if sequence == 2:
        prepare_stress90_execution_intent(
            store,
            target_trading_day="20260826",
            daily_decision_digest="2" * 64,
            account_identity_digest=_ACCOUNT_IDENTITY,
            account_epoch=_ACCOUNT_EPOCH,
            current_lots={},
            margin_fitted_lots={},
            symbol_products={},
        )
    expected = store.path.read_bytes()
    store.previous_path.write_bytes(expected)

    record = Stress90ExecutionIntentStore(store.path).load_required_record()

    assert record.sequence == sequence
    assert store.path.read_bytes() == expected


def test_same_day_execution_intent_rejects_account_lifecycle_epoch_change(
    tmp_path: Path,
):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    store = Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json")
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )

    with pytest.raises(
        Stress90ExecutionIntentIntegrityError,
        match="stale account lifecycle epoch",
    ):
        prepare_stress90_execution_intent(
            store,
            target_trading_day="20260825",
            daily_decision_digest="1" * 64,
            account_identity_digest=_ACCOUNT_IDENTITY,
            account_epoch="c" * 64,
            current_lots={},
            margin_fitted_lots={},
            symbol_products={},
        )


def test_verified_account_rebase_retires_intent_and_allows_same_day_new_epoch(
    tmp_path: Path,
) -> None:
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )
    from afuture.directional_stress90_state import REBASE_CONFIRMATION
    from afuture.stress90_lifecycle_transaction import derive_stress90_account_epoch

    store = Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json")
    original = prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={"A2612": 1},
        symbol_products={"A2612": "A"},
    )
    target_epoch = derive_stress90_account_epoch(
        operation="account_rebase",
        operation_nonce="e" * 64,
        policy_source_checksum="f" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        trading_day="20260825",
    )
    retired = store.retire_for_account_rebase(
        transaction_id="d" * 64,
        operation_nonce="e" * 64,
        policy_source_checksum="f" * 64,
        source_account_identity_digest=_ACCOUNT_IDENTITY,
        source_account_epoch=_ACCOUNT_EPOCH,
        target_account_identity_digest=_ACCOUNT_IDENTITY,
        target_account_epoch=target_epoch,
        trading_day="20260825",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        strong_confirmation=REBASE_CONFIRMATION,
    )

    assert retired is not None
    assert retired.retired is True
    assert retired.intent == original
    assert retired.effective_account_epoch == target_epoch
    replacement = prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=target_epoch,
        current_lots={},
        margin_fitted_lots={"A2612": 2},
        symbol_products={"A2612": "A"},
    )
    current = store.load_required_record()
    assert replacement.account_epoch == target_epoch
    assert replacement.initial_margin_fitted_lots == {"A2612": 2}
    assert current.retired is False
    assert current.sequence == 3


def test_verified_policy_reactivation_retires_old_account_epoch_intent(
    tmp_path: Path,
) -> None:
    from afuture.directional_policy_activation import STRESS90_ACTIVATION_CONFIRMATION
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )
    from afuture.directional_stress90_state import REBASE_CONFIRMATION
    from afuture.stress90_lifecycle_transaction import derive_stress90_account_epoch

    store = Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json")
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={"A2612": 1},
        symbol_products={"A2612": "A"},
    )
    target_epoch = derive_stress90_account_epoch(
        operation="reactivation",
        operation_nonce="e" * 64,
        policy_source_checksum="f" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        trading_day="20260825",
    )

    retired = store.retire_for_reactivation(
        transaction_id="d" * 64,
        operation_nonce="e" * 64,
        policy_source_checksum="f" * 64,
        source_account_identity_digest=_ACCOUNT_IDENTITY,
        source_account_epoch=_ACCOUNT_EPOCH,
        target_account_identity_digest=_ACCOUNT_IDENTITY,
        target_account_epoch=target_epoch,
        trading_day="20260825",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        activation_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
        rebase_confirmation=REBASE_CONFIRMATION,
    )

    assert retired is not None
    assert retired.retired is True
    assert retired.retirement is not None
    assert retired.retirement.operation == "reactivation"
    assert retired.effective_account_epoch == target_epoch


def test_execution_intent_rejects_duplicate_json_keys(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
    )

    path = tmp_path / "intent.json"
    path.write_text('{"kind":"one","kind":"two"}', encoding="utf-8")

    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="duplicate"):
        Stress90ExecutionIntentStore(path).load_required_record()


def test_execution_intent_missing_current_with_prev_evidence_fails_closed(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    path = tmp_path / "intent.json"
    store = Stress90ExecutionIntentStore(path)
    for day, digest in (("20260825", "1" * 64), ("20260826", "2" * 64)):
        prepare_stress90_execution_intent(
            store,
            target_trading_day=day,
            daily_decision_digest=digest,
            account_identity_digest=_ACCOUNT_IDENTITY,
            account_epoch=_ACCOUNT_EPOCH,
            current_lots={},
            margin_fitted_lots={},
            symbol_products={},
        )
    path.unlink()

    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="current.*missing"):
        store.load_record()


def test_execution_intent_prev_is_bound_to_current_checksum_and_sequence(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    path = tmp_path / "intent.json"
    store = Stress90ExecutionIntentStore(path)
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    older = path.read_bytes()
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260826",
        daily_decision_digest="2" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260827",
        daily_decision_digest="3" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    store.previous_path.write_bytes(older)

    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="parent|sequence"):
        store.load_required_record()


@pytest.mark.parametrize("failure_call", [1, 2])
def test_execution_intent_file_and_directory_fsync_failures_are_propagated(
    tmp_path: Path, monkeypatch, failure_call: int
):
    import afuture.directional_stress90_execution as execution
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    path = tmp_path / "intent.json"
    real_fsync = execution.os.fsync
    calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("injected execution intent fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(execution.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="intent fsync"):
        prepare_stress90_execution_intent(
            Stress90ExecutionIntentStore(path),
            target_trading_day="20260825",
            daily_decision_digest="1" * 64,
            account_identity_digest=_ACCOUNT_IDENTITY,
            account_epoch=_ACCOUNT_EPOCH,
            current_lots={},
            margin_fitted_lots={},
            symbol_products={},
        )
    monkeypatch.setattr(execution.os, "fsync", real_fsync)
    if failure_call == 1:
        assert not path.exists()
    else:
        assert Stress90ExecutionIntentStore(path).load_required_record().sequence == 1


def test_execution_intent_save_rejects_stale_expected_sequence(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    store = Stress90ExecutionIntentStore(tmp_path / "intent.json")
    intent = prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )

    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="sequence changed"):
        store.save(intent, expected_sequence=0)


def test_execution_intent_cross_instance_save_is_one_locked_cas(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    intent = prepare_stress90_execution_intent(
        Stress90ExecutionIntentStore(tmp_path / "template.json"),
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    path = tmp_path / "shared.json"
    stores = (Stress90ExecutionIntentStore(path), Stress90ExecutionIntentStore(path))
    barrier = Barrier(2)
    for store in stores:
        original = store.load_record

        def synchronized_load(original=original):
            record = original()
            barrier.wait(timeout=2.0)
            return record

        store.load_record = synchronized_load

    outcomes: list[str] = []

    def save(store: Stress90ExecutionIntentStore) -> None:
        try:
            store.save(intent, expected_sequence=0)
        except Stress90ExecutionIntentIntegrityError:
            outcomes.append("rejected")
        else:
            outcomes.append("saved")

    workers = [Thread(target=save, args=(store,)) for store in stores]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3.0)
        assert not worker.is_alive()

    assert sorted(outcomes) == ["rejected", "saved"]
    assert Stress90ExecutionIntentStore(path).load_required_record().sequence == 1


def test_candidate_target_sequence_uses_verified_sessions_not_business_day_guessing():
    from afuture.directional_stress90_runtime import stress90_target_transitions

    index = pd.DatetimeIndex(["2026-08-20", "2026-08-21", "2026-08-24"])
    assert stress90_target_transitions(
        last_completed_target_day="20260821",
        current_ctp_trading_day="20260825",
        completed_close_index=index,
    ) == (("20260821", "20260824"), ("20260824", "20260825"))

    with pytest.raises(RuntimeError, match="does not cover prior target"):
        stress90_target_transitions(
            last_completed_target_day="20260821",
            current_ctp_trading_day="20260825",
            completed_close_index=pd.DatetimeIndex(["2026-08-24"]),
        )
    with pytest.raises(RuntimeError, match="moved backward"):
        stress90_target_transitions(
            last_completed_target_day="20260825",
            current_ctp_trading_day="20260824",
            completed_close_index=index,
        )


def test_completed_account_continuity_uses_verified_ohlc_and_oi_session_chain(
    tmp_path: Path,
):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_oi_runtime import (
        ObservedTradingDayTransition,
        Stress90OiEvidenceState,
        Stress90OiEvidenceStore,
        build_fixed_historical_60m_evidence,
    )
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    def manager_with_sessions(
        root: Path,
        *,
        close_index: pd.DatetimeIndex,
        completed_oi_days: tuple[str, ...],
        observed_transitions: tuple[tuple[str, str], ...] = (),
    ) -> Stress90DirectionalPortfolioManager:
        root.mkdir()
        close = pd.DataFrame(100.0, index=close_index, columns=FROZEN_PRODUCTS)
        ohlc_path = root / "directional_ohlc_cache.json"
        DirectionalOHLCCacheStore(ohlc_path).save(FROZEN_PRODUCTS, close, close)
        completed = []
        for day in completed_oi_days:
            rows = [
                {
                    "datetime": datetime.strptime(day + " 09:00", "%Y%m%d %H:%M").replace(
                        tzinfo=_CHINA
                    ),
                    "product": product,
                    "symbol": f"{product}2612",
                    "open": 100.0,
                    "close": 101.0,
                    "volume": 10.0,
                    "hold": 100.0,
                }
                for product in ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")
            ]
            completed.append(build_fixed_historical_60m_evidence(day, rows))
        oi_path = root / "stress90_oi_evidence.json"
        by_day = {item.trading_day: item for item in completed}
        Stress90OiEvidenceStore(oi_path).save_state(
            Stress90OiEvidenceState(
                completed=tuple(completed),
                observed_transitions=tuple(
                    ObservedTradingDayTransition(
                        source_trading_day=source,
                        target_trading_day=target,
                        completed_oi_evidence_digest=by_day[source].evidence_digest,
                    )
                    for source, target in observed_transitions
                ),
            )
        )
        return Stress90DirectionalPortfolioManager(
            DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
            SimpleNamespace(set_raw_tick_observer=lambda _observer: None),
            RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
            historical_mode=True,
            policy_state_path=root / "state.json",
            seed_path=root / "seed.json",
            oi_evidence_path=oi_path,
            ohlc_cache_path=ohlc_path,
        )

    adjacent_history = pd.date_range(end="2026-08-25", periods=170, freq="D")
    adjacent = manager_with_sessions(
        tmp_path / "adjacent",
        close_index=adjacent_history,
        completed_oi_days=("20260825",),
        observed_transitions=(("20260825", "20260826"),),
    )
    friday_history = pd.date_range(end="2026-08-21", periods=170, freq="D")
    missing_transition = manager_with_sessions(
        tmp_path / "missing-transition",
        close_index=friday_history,
        completed_oi_days=("20260821",),
    )
    gap_history = pd.date_range(end="2026-08-26", periods=170, freq="D")
    gap = manager_with_sessions(
        tmp_path / "gap",
        close_index=gap_history,
        completed_oi_days=("20260824", "20260825", "20260826"),
    )
    try:
        continuity = adjacent.completed_account_day_continuity_evidence(
            "20260825",
            "20260826",
        )
        assert continuity.completed_account_day == "20260825"
        assert continuity.current_ctp_trading_day == "20260826"
        assert len(continuity.ohlc_content_digest) == 64
        assert len(continuity.oi_store_checksum) == 64
        assert len(continuity.completed_oi_evidence_digest) == 64
        assert len(continuity.observed_transition_digest) == 64
        assert len(continuity.continuity_digest) == 64
        assert adjacent.completed_account_day_is_contiguous("20260825", "20260826") is True
        assert (
            missing_transition.completed_account_day_is_contiguous(
                "20260821",
                "20260825",
            )
            is False
        )
        assert gap.completed_account_day_is_contiguous("20260824", "20260827") is False
    finally:
        adjacent.close()
        missing_transition.close()
        gap.close()


def test_completed_account_continuity_rejects_nonadjacent_endpoint_evidence(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_oi_runtime import (
        ObservedTradingDayTransition,
        Stress90OiEvidenceRecord,
        Stress90OiEvidenceState,
        build_fixed_historical_60m_evidence,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_runtime import (
        load_stress90_account_day_continuity_evidence,
    )

    close_index = pd.date_range(end="2026-08-21", periods=170, freq="D")
    close = pd.DataFrame(100.0, index=close_index, columns=STRESS90_POLICY.products)
    ohlc_store = DirectionalOHLCCacheStore(tmp_path / "directional_ohlc_cache.json")
    ohlc_store.save(STRESS90_POLICY.products, close, close)
    completed = build_fixed_historical_60m_evidence(
        "20260821",
        [
            {
                "datetime": datetime(2026, 8, 21, 9, 0, tzinfo=_CHINA),
                "product": product,
                "symbol": f"{product}2612",
                "open": 100.0,
                "close": 101.0,
                "volume": 10.0,
                "hold": 100.0,
            }
            for product in STRESS90_POLICY.oi_products
        ],
    )
    oi_record = Stress90OiEvidenceRecord(
        state=Stress90OiEvidenceState(
            completed=(completed,),
            observed_transitions=(
                ObservedTradingDayTransition(
                    source_trading_day="20260821",
                    target_trading_day="20260824",
                    completed_oi_evidence_digest=completed.evidence_digest,
                ),
            ),
        ),
        sequence=1,
        checksum="a" * 64,
    )

    with pytest.raises(RuntimeError, match="official immutable session ledger"):
        load_stress90_account_day_continuity_evidence(
            ohlc_store,
            SimpleNamespace(load_required_record=lambda: oi_record),
            completed_account_day="20260821",
            current_ctp_trading_day="20260824",
        )


def test_manager_validates_required_policy_state_before_market_subscriptions(tmp_path: Path):
    from afuture.directional import DirectionalConfig
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    class Broker:
        metadata_query_blocks = False

        def __init__(self):
            self.calls: list[str] = []

        def get_contract_catalog(self):
            self.calls.append("catalog")
            return []

        def get_trading_day(self):
            self.calls.append("trading_day")
            return "20260825"

        def subscribe(self, symbol, exchange):
            self.calls.append(f"subscribe:{symbol}:{exchange}")

    broker = Broker()
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        broker,
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        historical_mode=True,
        policy_state_path=tmp_path / "stress90_policy_state.json",
        seed_path=tmp_path / "stress90_bootstrap_seed.json",
        oi_evidence_path=tmp_path / "stress90_oi_evidence.json",
    )

    with pytest.raises(RuntimeError, match="required Stress-90 policy state"):
        manager.bootstrap(datetime(2026, 8, 24, 20, 30, tzinfo=_CHINA))
    assert broker.calls == []


def test_manager_rejects_any_risk_envelope_looser_than_stress90(tmp_path: Path):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    with pytest.raises(ValueError, match="hard risk envelope"):
        Stress90DirectionalPortfolioManager(
            DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
            SimpleNamespace(),
            RiskManager(
                RiskConfig(
                    max_margin_ratio=0.36,
                    margin_estimate_buffer=1.25,
                )
            ),
            historical_mode=True,
            policy_state_path=tmp_path / "state.json",
            seed_path=tmp_path / "seed.json",
            oi_evidence_path=tmp_path / "oi.json",
        )


def test_stress90_live_metadata_query_runs_only_in_background(tmp_path: Path):
    from afuture.directional import DirectionalConfig
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import ContractSpec
    from afuture.risk import RiskConfig, RiskManager

    started = Event()
    release = Event()
    worker_thread: list[int] = []

    class SlowBroker:
        metadata_query_blocks = True

        def get_trading_day(self):
            return "20260825"

        def get_live_contract_specs(self, symbols, timeout_seconds=10.0):
            del timeout_seconds
            worker_thread.append(get_ident())
            started.set()
            release.wait(1.0)
            return {
                symbol: ContractSpec(symbol, "DCE", 10.0, 1.0, 0.10, 0.10) for symbol in symbols
            }

    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        SlowBroker(),
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        historical_mode=True,
        policy_state_path=tmp_path / "state.json",
        seed_path=tmp_path / "seed.json",
        oi_evidence_path=tmp_path / "oi.json",
    )
    caller_thread = get_ident()
    before = monotonic()
    try:
        with pytest.raises(RuntimeError, match="background live metadata"):
            manager._ensure_specs({"A2612"})
        assert monotonic() - before < 0.2
        assert started.wait(0.5)
        assert worker_thread != [caller_thread]
        release.set()
        deadline = monotonic() + 1.0
        while True:
            try:
                specs = manager._ensure_specs({"A2612"})
                break
            except RuntimeError:
                if monotonic() >= deadline:
                    raise
                sleep(0.01)
        assert specs["A2612"].margin_rate_long == 0.10
    finally:
        release.set()
        refresher = getattr(manager, "_metadata_prefetcher", None)
        if refresher is not None:
            refresher.close()


def test_base_weight_transition_uses_exact_authoritative_target_index(tmp_path: Path):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    class SpyPolicy:
        def __init__(self):
            self.open_index = None
            self.close_index = None

        def target_weights(self, open_prices, close):
            self.open_index = open_prices.index.copy()
            self.close_index = close.index.copy()
            return {product: 0.0 for product in FROZEN_PRODUCTS}

    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        SimpleNamespace(),
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        historical_mode=True,
        policy_state_path=tmp_path / "state.json",
        seed_path=tmp_path / "seed.json",
        oi_evidence_path=tmp_path / "oi.json",
    )
    spy = SpyPolicy()
    manager.policy = spy
    index = pd.date_range("2026-01-01", periods=170, freq="D")
    close = pd.DataFrame(100.0, index=index, columns=FROZEN_PRODUCTS)
    entry = SimpleNamespace(open=close - 1.0, close=close)

    weights = manager._base_weights_for_transition(
        entry,
        completed_input_day=index[-2].strftime("%Y%m%d"),
        target_trading_day="20260930",
    )

    assert set(weights) == set(FROZEN_PRODUCTS)
    assert spy.open_index[-1] == pd.Timestamp("2026-09-30")
    assert spy.close_index[-1] == pd.Timestamp("2026-09-30")
    assert spy.close_index[-2] == index[-2]


def test_runtime_prepares_candidate_once_from_cache_and_completed_oi(tmp_path: Path):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.directional_stress90_state import Stress90PolicyStateStore
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    seed_path, state_path = _write_seed_state(tmp_path)
    oi_path = _write_completed_oi(tmp_path)
    ohlc_path = tmp_path / "directional_ohlc_cache.json"
    index = pd.date_range(end="2026-08-24", periods=170, freq="D")
    values = {
        product: [100.0 * (1.001**row) for row in range(len(index))] for product in FROZEN_PRODUCTS
    }
    close = pd.DataFrame(values, index=index)
    DirectionalOHLCCacheStore(ohlc_path).save(FROZEN_PRODUCTS, close / 1.001, close)
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        SimpleNamespace(),
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        historical_mode=True,
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=oi_path,
        ohlc_cache_path=ohlc_path,
    )

    class Base:
        def target_weights(self, open_prices, close_prices):
            del open_prices, close_prices
            return {
                product: (1.0 if product in {"A", "AG"} else 0.0) for product in FROZEN_PRODUCTS
            }

    manager.policy = Base()
    first = manager._prepare_decision_for_current_day("20260825")
    second = manager._prepare_decision_for_current_day("20260825")
    record = Stress90PolicyStateStore(state_path).load_required_record()

    assert first == second
    assert first.target_trading_day == "20260825"
    assert first.input_days == {"completed_close": "20260824", "completed_oi": "20260824"}
    assert record.sequence == 2
    assert record.state.completed_concentrations == (0.5,)


def test_runtime_refuses_candidate_when_ctp_rollover_was_not_observed(tmp_path: Path):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore
    from afuture.directional_stress90_runtime import (
        Stress90DataUnavailableError,
        Stress90DirectionalPortfolioManager,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    seed_path, state_path = _write_seed_state(tmp_path)
    oi_path = _write_completed_oi(tmp_path)
    oi_store = Stress90OiEvidenceStore(oi_path)
    oi_record = oi_store.load_required_record()
    oi_store.save_state(
        replace(oi_record.state, observed_transitions=()),
        expected_sequence=oi_record.sequence,
    )
    ohlc_path = tmp_path / "directional_ohlc_cache.json"
    index = pd.date_range(end="2026-08-24", periods=170, freq="D")
    close = pd.DataFrame(100.0, index=index, columns=FROZEN_PRODUCTS)
    DirectionalOHLCCacheStore(ohlc_path).save(FROZEN_PRODUCTS, close, close)
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        SimpleNamespace(),
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        historical_mode=True,
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=oi_path,
        ohlc_cache_path=ohlc_path,
    )
    manager.policy = SimpleNamespace(
        target_weights=lambda _open, _close: dict.fromkeys(FROZEN_PRODUCTS, 0.0)
    )

    with pytest.raises(Stress90DataUnavailableError, match="rollover"):
        manager._prepare_decision_for_current_day("20260825")


def test_halted_candidate_preparer_has_no_order_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_stress90_runtime import (
        Stress90DirectionalPortfolioManager,
        prepare_halted_stress90_candidate,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig

    expected = SimpleNamespace(
        target_trading_day="20260825",
        daily_decision_digest="d" * 64,
    )

    def prepare_without_order(self, current, *, required_activity_day=None):
        assert current == "20260825"
        assert required_activity_day == "20260824"
        with pytest.raises(RuntimeError, match="cannot send orders"):
            self.broker.send_order(object())
        return expected

    monkeypatch.setattr(
        Stress90DirectionalPortfolioManager,
        "_prepare_decision_for_current_day",
        prepare_without_order,
    )

    prepared = prepare_halted_stress90_candidate(
        directional_config=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
        ),
        risk_config=RiskConfig(margin_estimate_buffer=1.25),
        runtime_dir=tmp_path,
        current_ctp_trading_day="20260825",
        required_activity_day="20260824",
    )

    assert prepared is expected


def test_runtime_rejects_ohlc_calendar_that_silently_omits_completed_oi_session(tmp_path: Path):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceState,
        Stress90OiEvidenceStore,
        build_fixed_historical_60m_evidence,
    )
    from afuture.directional_stress90_runtime import (
        Stress90DataUnavailableError,
        Stress90DirectionalPortfolioManager,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    seed_path, state_path = _write_seed_state(tmp_path, last_target_day="20260824")
    rows = []
    for day in ("20260824", "20260825", "20260826"):
        evidence_rows = [
            {
                "datetime": datetime.strptime(day + " 09:00", "%Y%m%d %H:%M").replace(
                    tzinfo=_CHINA
                ),
                "product": product,
                "symbol": f"{product}2612",
                "open": 100.0,
                "close": 100.0,
                "volume": 10.0,
                "hold": 100.0,
            }
            for product in ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")
        ]
        rows.append(build_fixed_historical_60m_evidence(day, evidence_rows))
    oi_path = tmp_path / "stress90_oi_evidence.json"
    Stress90OiEvidenceStore(oi_path).save_state(Stress90OiEvidenceState(completed=tuple(rows)))

    index = pd.date_range(end="2026-08-26", periods=170, freq="D")
    index = index[index != pd.Timestamp("2026-08-25")]
    close = pd.DataFrame(100.0, index=index, columns=FROZEN_PRODUCTS)
    ohlc_path = tmp_path / "directional_ohlc_cache.json"
    DirectionalOHLCCacheStore(ohlc_path).save(FROZEN_PRODUCTS, close, close)
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        SimpleNamespace(),
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        historical_mode=True,
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=oi_path,
        ohlc_cache_path=ohlc_path,
    )

    with pytest.raises(Stress90DataUnavailableError, match="calendar|session|align"):
        manager._prepare_decision_for_current_day("20260827")


def test_fresh_seed_on_bootstrap_target_day_holds_without_orders(tmp_path: Path):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_activity import ContractActivity, DirectionalActivitySnapshot
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    seed_path, state_path = _write_seed_state(tmp_path, last_target_day="20260825")
    _bind_seed_state_to_test_account(state_path)
    activity = DirectionalActivitySnapshot(
        "20260824",
        {
            "A2612": ContractActivity(
                "A2612",
                "DCE",
                "A",
                "20260824",
                20_000.0,
                30_000.0,
                datetime(2026, 8, 24, 14, 59, tzinfo=_CHINA),
            )
        },
    )

    class Broker:
        def __init__(self):
            self.orders = []

        def is_ready(self):
            return True

        def get_active_orders(self):
            return []

        def get_trading_day(self):
            return "20260825"

        def get_account_identity_digest(self):
            return _ACCOUNT_IDENTITY

        def get_positions(self):
            return []

        def send_order(self, request):
            self.orders.append(request)
            return f"order-{len(self.orders)}"

    broker = Broker()
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        broker,
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        historical_mode=True,
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=tmp_path / "stress90_oi_evidence.json",
        activity_tracker=SimpleNamespace(completed_snapshot=activity),
    )
    manager._initialized = True

    result = manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))

    assert result.action == "hold"
    assert "bootstrap target" in result.reason
    assert result.order_ids == ()
    assert broker.orders == []


def _mechanical_manager(
    tmp_path: Path,
    *,
    target_weight: float,
    positions=None,
    concentration_freeze: bool = False,
    quality_recorder=None,
    max_gross_leverage: float = 2.0,
):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_activity import ContractActivity, DirectionalActivitySnapshot
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot, ContractInfo, ContractSpec, Tick
    from afuture.risk import RiskConfig, RiskManager

    seed_path, state_path = _write_seed_state(tmp_path)
    _bind_seed_state_to_test_account(state_path)
    intent_path = tmp_path / "stress90_execution_intent.json"
    now = datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA)
    contract = ContractInfo("A2612", "DCE", "A", "2026-12-15")
    tick = Tick(
        "A2612",
        "DCE",
        now,
        999.0,
        1001.0,
        1000.0,
        1000.0,
        1000.0,
        "20260825",
        volume=20_000.0,
        open_interest=30_000.0,
    )
    spec = ContractSpec("A2612", "DCE", 10.0, 1.0, 0.10, 0.10)
    activity = DirectionalActivitySnapshot(
        "20260824",
        {
            "A2612": ContractActivity(
                "A2612",
                "DCE",
                "A",
                "20260824",
                20_000.0,
                30_000.0,
                now,
            )
        },
    )

    class Broker:
        metadata_query_blocks = False

        def __init__(self):
            self.positions = list(positions or [])
            self.orders = []
            self.active_orders = []
            self.intent_path = intent_path

        def is_ready(self):
            return True

        def get_active_orders(self):
            return list(self.active_orders)

        def get_positions(self):
            return list(self.positions)

        def get_trading_day(self):
            return "20260825"

        def get_account_identity_digest(self):
            return _ACCOUNT_IDENTITY

        def get_account(self):
            return AccountSnapshot(
                100_000.0,
                100_000.0,
                100_000.0,
                0.0,
                0.0,
                0.0,
                "20260825",
            )

        def send_order(self, request):
            assert self.intent_path.exists()
            self.orders.append(request)
            return f"order-{len(self.orders)}"

    broker = Broker()
    risk = RiskManager(
        RiskConfig(
            max_margin_ratio=0.35,
            min_available_ratio=0.25,
            max_daily_loss_ratio=0.05,
            max_total_drawdown_ratio=0.30,
            max_contract_volume=35,
            margin_estimate_buffer=1.25,
        )
    )
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            max_gross_leverage=max_gross_leverage,
        ),
        broker,
        risk,
        historical_mode=True,
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=tmp_path / "oi.json",
        execution_intent_path=intent_path,
        activity_tracker=SimpleNamespace(completed_snapshot=activity),
        static_specs={"A2612": spec},
        quality_recorder=quality_recorder,
    )
    weights = {product: 0.0 for product in STRESS90_POLICY.products}
    weights["A"] = target_weight
    prepared = SimpleNamespace(
        previous_target_trading_day="20260824",
        target_trading_day="20260825",
        daily_decision_digest="d" * 64,
        base_weights=weights,
        oi_confirmed_weights=weights,
        cost_approved_weights=weights,
        survivor_weights=weights,
        current_hhi=1.0,
        prior_hhi_median=0.5,
        concentration_freeze=concentration_freeze,
        input_days={"completed_close": "20260824", "completed_oi": "20260824"},
    )
    manager._prepare_decision_for_current_day = lambda current, **kwargs: prepared
    manager._initialized = True
    manager._catalog = [contract]
    manager._catalog_by_symbol = {contract.symbol: contract}
    manager._ticks = {tick.symbol: tick}
    return manager, broker, intent_path, now


def test_final_opening_batch_rechecks_strict_gross_with_fresh_broker_truth(tmp_path: Path):
    from afuture.models import Offset, OrderRequest, OrderSide, OrderType

    manager, broker, _intent_path, now = _mechanical_manager(
        tmp_path,
        target_weight=1.0,
        max_gross_leverage=1.0,
    )
    request = OrderRequest(
        symbol="A2612",
        exchange="DCE",
        side=OrderSide.BUY,
        offset=Offset.OPEN,
        volume=11,
        price=1000.0,
        order_type=OrderType.FAK,
        reference="directional:A",
    )

    reason = manager._opening_policy_rejection(
        broker.get_account(),
        [],
        [request],
        manager._specs,
        now,
    )

    assert reason == "Stress-90 opening batch would exceed configured gross limit"


def test_runtime_records_one_complete_quality_decision_before_first_order(tmp_path: Path):
    from afuture.quality import ExecutionQualityRecorder

    recorder = ExecutionQualityRecorder(tmp_path / "quality.jsonl")
    manager, broker, _intent_path, now = _mechanical_manager(
        tmp_path,
        target_weight=1.0,
        quality_recorder=recorder,
    )

    first = manager.maybe_rebalance(now)
    second = manager.maybe_rebalance(now)

    assert first.action == "open"
    assert second.action == "open"
    rows = [row for row in recorder._read() if row.get("event") == "stress90_decision"]
    assert len(rows) == 1
    assert rows[0]["stress90_margin_fitted_target"] == {"A2612": 10}
    assert rows[0]["stress90_decision_digest"] == "d" * 64
    assert len(broker.orders) == 2


def test_runtime_persists_execution_intent_before_order_and_reuses_it_after_partial_fill(
    tmp_path: Path,
):
    from afuture.directional_stress90_execution import Stress90ExecutionIntentStore
    from afuture.models import ContractPosition

    manager, broker, intent_path, now = _mechanical_manager(tmp_path, target_weight=1.0)

    first = manager.maybe_rebalance(now)
    intent = Stress90ExecutionIntentStore(intent_path).load_required_record()
    assert first.action == "open"
    assert intent.sequence == 1
    assert intent.intent.initial_margin_fitted_lots == {"A2612": 10}

    broker.positions = [ContractPosition("A2612", "DCE", long_today=3, long_price=1000.0)]
    second = manager.maybe_rebalance(now)

    assert second.action == "open"
    assert broker.orders[-1].volume == 7
    assert Stress90ExecutionIntentStore(intent_path).load_required_record().sequence == 1


def test_runtime_intent_persistence_failure_sends_no_orders(tmp_path: Path):
    manager, broker, _intent_path, now = _mechanical_manager(tmp_path, target_weight=1.0)

    def fail_save(_intent, **_kwargs):
        raise RuntimeError("intent fsync failed")

    manager.execution_intent_store.save = fail_save
    with pytest.raises(RuntimeError, match="intent fsync failed"):
        manager.maybe_rebalance(now)
    assert broker.orders == []


def test_runtime_reversal_is_reduction_first_then_opens_from_persisted_intent(
    tmp_path: Path,
):
    from afuture.models import ContractPosition, Offset, OrderSide

    manager, broker, _intent_path, now = _mechanical_manager(
        tmp_path,
        target_weight=-1.0,
        positions=[ContractPosition("A2612", "DCE", long_today=2, long_price=1000.0)],
        concentration_freeze=True,
    )

    first = manager.maybe_rebalance(now)
    assert first.action == "reduce"
    assert all(order.offset is not Offset.OPEN for order in broker.orders)

    broker.positions = []
    second = manager.maybe_rebalance(now)
    assert second.action == "open"
    assert broker.orders[-1].offset is Offset.OPEN
    assert broker.orders[-1].side is OrderSide.SELL
    assert broker.orders[-1].volume == 2

    broker.positions = [ContractPosition("A2612", "DCE", short_today=1, short_price=1000.0)]
    third = manager.maybe_rebalance(now)
    assert third.action == "open"
    assert broker.orders[-1].volume == 1


def test_runtime_missed_first_window_never_chases_new_entry(tmp_path: Path):
    manager, broker, intent_path, now = _mechanical_manager(tmp_path, target_weight=1.0)

    result = manager.maybe_rebalance(now.replace(hour=22))

    assert result.action == "hold"
    assert "entry window" in result.reason
    assert intent_path.exists()
    assert broker.orders == []
