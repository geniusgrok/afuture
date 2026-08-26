import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

_SOURCE_MANIFEST = {
    "broad_daily_universe.csv": "1" * 64,
    "return_target_specific_contracts.csv": "2" * 64,
    "execution_aligned_weights.csv": "3" * 64,
    "prior_two_year_broad_60m.csv": "4" * 64,
    "two_year_broad_60m.csv": "5" * 64,
}


def _checksum_envelope(raw: dict) -> str:
    unsigned = {key: value for key, value in raw.items() if key != "checksum"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _seed_and_state():
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        Stress90CandidateState,
    )
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
    )

    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260824",
        last_decision_digest="a" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest=_SOURCE_MANIFEST,
        bootstrap_through_day="20260824",
        last_completed_input_day="20260821",
    )
    return seed, Stress90PolicyState.from_seed(seed)


def _close_path(daily_return: float = 0.001) -> tuple[float, ...]:
    values = [100.0]
    for _ in range(20):
        values.append(values[-1] * (1.0 + daily_return))
    return tuple(values)


def _decision_inputs():
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_state import Stress90DecisionInputs

    base = {product: 0.0 for product in STRESS90_POLICY.products}
    base.update(A=1.0, AG=1.0)
    flow = {product: 0 for product in STRESS90_POLICY.oi_products}
    flow["A"] = 1
    return Stress90DecisionInputs(
        previous_target_trading_day="20260824",
        target_trading_day="20260825",
        base_weights=base,
        completed_close_history={product: _close_path() for product in STRESS90_POLICY.products},
        completed_oi_flow=flow,
        completed_close_day="20260824",
        completed_oi_day="20260824",
    )


def test_policy_state_store_uses_sequence_checksum_atomic_prev_and_no_fallback(tmp_path: Path):
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90StateIntegrityError,
        record_completed_account_day,
    )

    _seed, state = _seed_and_state()
    store = Stress90PolicyStateStore(tmp_path / "stress90_state.json")
    first = store.save(state)
    updated = record_completed_account_day(state, "20260825", 0.01)
    second = store.save(updated)

    assert first.sequence == 1
    assert second.sequence == 2
    assert store.previous_path.exists()
    assert store.load_previous_record().sequence == 1
    assert store.load_required().completed_account_wealth == 1.01

    store.path.write_bytes(b"corrupt")
    with pytest.raises(Stress90StateIntegrityError, match="JSON"):
        store.load_required()
    assert store.load_previous_record().state == state


@pytest.mark.parametrize("evidence_suffix", [".prev", ".lock"])
def test_policy_state_store_missing_current_with_evidence_fails_load_and_save(
    tmp_path: Path,
    evidence_suffix: str,
):
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90StateIntegrityError,
    )

    _seed, state = _seed_and_state()
    path = tmp_path / "stress90_state.json"
    evidence_path = path.with_name(f"{path.name}{evidence_suffix}")
    evidence_path.write_bytes(b"incident evidence")
    store = Stress90PolicyStateStore(path)

    with pytest.raises(Stress90StateIntegrityError, match="current.*missing"):
        store.load_record()
    with pytest.raises(Stress90StateIntegrityError, match="current.*missing"):
        store.save(state)

    assert not path.exists()
    assert evidence_path.read_bytes() == b"incident evidence"


def test_policy_state_store_rejects_checksum_schema_sequence_and_duplicate_keys(tmp_path: Path):
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90StateIntegrityError,
    )

    _seed, state = _seed_and_state()
    path = tmp_path / "stress90_state.json"
    store = Stress90PolicyStateStore(path)
    store.save(state)

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["state"]["completed_account_wealth"] = 0.9
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Stress90StateIntegrityError, match="checksum"):
        store.load_required()

    raw["schema_version"] = 999
    raw["checksum"] = _checksum_envelope(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Stress90StateIntegrityError, match="schema"):
        store.load_required()

    from afuture.directional_stress90_state import STRESS90_STATE_SCHEMA_VERSION

    raw["schema_version"] = STRESS90_STATE_SCHEMA_VERSION
    raw["sequence"] = 0
    raw["checksum"] = _checksum_envelope(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Stress90StateIntegrityError, match="sequence"):
        store.load_required()

    path.write_text('{"kind":"one","kind":"two"}', encoding="utf-8")
    with pytest.raises(Stress90StateIntegrityError, match="duplicate"):
        store.load_required()


def test_policy_identity_and_seed_mismatch_fail_closed(tmp_path: Path):
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90StateIntegrityError,
    )

    _seed, state = _seed_and_state()
    store = Stress90PolicyStateStore(tmp_path / "stress90_state.json")

    with pytest.raises(Stress90StateIntegrityError, match="policy definition digest"):
        store.save(replace(state, policy_definition_digest="0" * 64))
    with pytest.raises(Stress90StateIntegrityError, match="products manifest digest"):
        store.save(replace(state, products_manifest_digest="0" * 64))
    with pytest.raises(Stress90StateIntegrityError, match="bootstrap seed digest"):
        store.save(replace(state, bootstrap_seed_digest="0" * 64))


def test_same_target_called_one_hundred_times_advances_and_persists_once(tmp_path: Path):
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        prepare_stress90_decision,
    )

    _seed, state = _seed_and_state()
    store = Stress90PolicyStateStore(tmp_path / "stress90_state.json")
    store.save(state)
    prepared = [prepare_stress90_decision(store, _decision_inputs()) for _ in range(100)]
    record = store.load_required_record()

    assert len({item.daily_decision_digest for item in prepared}) == 1
    assert record.sequence == 2
    assert record.state.last_completed_target_day == "20260825"
    assert record.state.completed_concentrations == (0.5,)
    assert record.state.prepared_decision == prepared[0]


def test_same_target_with_changed_input_fails_instead_of_recomputing(tmp_path: Path):
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90StateIntegrityError,
        prepare_stress90_decision,
    )

    _seed, state = _seed_and_state()
    store = Stress90PolicyStateStore(tmp_path / "stress90_state.json")
    store.save(state)
    prepare_stress90_decision(store, _decision_inputs())
    inputs = _decision_inputs()
    changed = dict(inputs.base_weights)
    changed.update(A=0.5, AG=1.5)

    with pytest.raises(Stress90StateIntegrityError, match="same-day input digest"):
        prepare_stress90_decision(store, replace(inputs, base_weights=changed))
    assert store.load_required_record().sequence == 2


def test_restart_reuses_saved_decision_without_reappending_hhi(tmp_path: Path):
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        prepare_stress90_decision,
    )

    _seed, state = _seed_and_state()
    path = tmp_path / "stress90_state.json"
    first_store = Stress90PolicyStateStore(path)
    first_store.save(state)
    first = prepare_stress90_decision(first_store, _decision_inputs())

    restarted = Stress90PolicyStateStore(path)
    second = prepare_stress90_decision(restarted, _decision_inputs())

    assert second == first
    assert restarted.load_required_record().sequence == 2
    assert restarted.load_required().completed_concentrations == (0.5,)


def test_persistence_failure_does_not_expose_an_unpersisted_decision(tmp_path: Path):
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        prepare_stress90_decision,
    )

    _seed, state = _seed_and_state()
    path = tmp_path / "stress90_state.json"
    Stress90PolicyStateStore(path).save(state)

    class _FailingStore(Stress90PolicyStateStore):
        def save(self, state, *, expected_sequence=None):
            raise OSError("injected persistence failure")

    with pytest.raises(OSError, match="injected persistence failure"):
        prepare_stress90_decision(_FailingStore(path), _decision_inputs())
    restored = Stress90PolicyStateStore(path).load_required_record()
    assert restored.sequence == 1
    assert restored.state.prepared_decision is None


def test_target_gap_is_rejected_before_policy_state_advances(tmp_path: Path):
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90StateIntegrityError,
        prepare_stress90_decision,
    )

    _seed, state = _seed_and_state()
    store = Stress90PolicyStateStore(tmp_path / "stress90_state.json")
    store.save(state)
    gap = replace(_decision_inputs(), previous_target_trading_day="20260823")

    with pytest.raises(Stress90StateIntegrityError, match="target-day gap"):
        prepare_stress90_decision(store, gap)
    assert store.load_required_record().sequence == 1


def test_seed_store_is_immutable_checksummed_and_identity_checked(tmp_path: Path):
    from afuture.directional_stress90_state import (
        Stress90SeedStore,
        Stress90StateIntegrityError,
    )

    seed, _state = _seed_and_state()
    path = tmp_path / "stress90_seed.json"
    store = Stress90SeedStore(path)
    store.save_new(seed)

    assert store.load_required() == seed
    with pytest.raises(Stress90StateIntegrityError, match="immutable"):
        store.save_new(seed)
    with pytest.raises(Stress90StateIntegrityError, match="source manifest"):
        store.load_required(expected_source_manifest={**_SOURCE_MANIFEST, "extra": "6" * 64})

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["seed"]["bootstrap_through_day"] = "20260825"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Stress90StateIntegrityError, match="checksum"):
        store.load_required()


def test_execution_aligned_runtime_state_is_never_accepted_as_stress90_state(tmp_path: Path):
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90StateIntegrityError,
    )
    from afuture.state import RuntimeState, StateStore

    path = tmp_path / "directional_state.json"
    StateStore(path).save(RuntimeState())

    with pytest.raises(Stress90StateIntegrityError, match="envelope"):
        Stress90PolicyStateStore(path).load_required()
