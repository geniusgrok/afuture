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
        bootstrap_gap_manifest={
            "broad_daily_missing": ("2024-01-30/AP",),
            "target_day_skips": ("2022-09-21",),
        },
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


def test_permanent_reserve_survives_process_restart_and_cannot_be_cleared(tmp_path: Path):
    import subprocess
    import sys

    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90StateIntegrityError,
        record_completed_account_day,
    )

    _seed, state = _seed_and_state()
    store = Stress90PolicyStateStore(tmp_path / "state.json")
    store.save(state)
    triggered = record_completed_account_day(state, "20260825", -0.25)
    store.save(triggered, expected_sequence=1)
    # A fresh interpreter uses the production reader/writer after the saved trigger.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            "from afuture.directional_stress90_state import "
            "Stress90PolicyStateStore, record_completed_account_day, "
            "drawdown_reserve_triggered_from_state; "
            "s=Stress90PolicyStateStore(sys.argv[1]); r=s.load_required_record(); "
            "assert drawdown_reserve_triggered_from_state(r.state); "
            "u=record_completed_account_day(r.state, '20260826', 0.4); "
            "assert u.completed_account_wealth == u.completed_account_high_watermark; "
            "assert drawdown_reserve_triggered_from_state(u); "
            "s.save(u, expected_sequence=r.sequence)",
            str(store.path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    recovered = store.load_required_record()
    assert recovered.sequence == 3
    assert recovered.state.completed_account_reserve_triggered
    before = (store.path.read_bytes(), store.previous_path.read_bytes())
    with pytest.raises(Stress90StateIntegrityError, match="cannot be cleared"):
        store.save(
            replace(recovered.state, completed_account_reserve_triggered=False),
            expected_sequence=recovered.sequence,
        )
    assert (store.path.read_bytes(), store.previous_path.read_bytes()) == before


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
    with pytest.raises(Stress90StateIntegrityError, match="bootstrap seed digest"):
        store.save(
            replace(
                state,
                bootstrap_gap_manifest={"target_day_skips": ("2022-09-22",)},
            )
        )


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
