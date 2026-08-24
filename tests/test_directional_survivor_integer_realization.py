from pathlib import Path


def _api():
    from afuture.directional_survivor_integer_realization import (
        SurvivorIntegerDirectionalProductionAcceptance,
        recover_floor_ceil_rounding,
    )

    return SurvivorIntegerDirectionalProductionAcceptance, recover_floor_ceil_rounding


def test_recovery_uses_only_floor_or_one_ceil_and_preserves_direction():
    _, recover = _api()
    result = recover(
        floor_lots={"A2501": 1, "M2501": -1},
        ideal_lots={"A2501": 1.7, "M2501": -1.3},
        lot_notionals={"A2501": 100.0, "M2501": 100.0},
        per_lot_margin={"A2501": 10.0, "M2501": 10.0},
        gross_budget=300.0,
        margin_budget=30.0,
        max_contract_volume=35,
    )
    assert result == {"A2501": 2, "M2501": -1}


def test_recovery_cannot_spend_more_than_continuous_gross_residual():
    _, recover = _api()
    result = recover(
        floor_lots={},
        ideal_lots={"AU2506": 0.6},
        lot_notionals={"AU2506": 100.0},
        per_lot_margin={"AU2506": 10.0},
        gross_budget=60.0,
        margin_budget=30.0,
        max_contract_volume=35,
    )
    assert result == {}


def test_recovery_cannot_cross_soft_margin_budget():
    _, recover = _api()
    result = recover(
        floor_lots={"A2501": 1},
        ideal_lots={"A2501": 1.8},
        lot_notionals={"A2501": 100.0},
        per_lot_margin={"A2501": 30.0},
        gross_budget=180.0,
        margin_budget=50.0,
        max_contract_volume=35,
    )
    assert result == {"A2501": 1}


def test_equal_fractional_tie_is_deterministic_and_half_rounds_down():
    _, recover = _api()
    result = recover(
        floor_lots={},
        ideal_lots={"M2501": 0.8, "A2501": 0.8},
        lot_notionals={"M2501": 100.0, "A2501": 100.0},
        per_lot_margin={"M2501": 10.0, "A2501": 10.0},
        gross_budget=160.0,
        margin_budget=30.0,
        max_contract_volume=35,
    )
    # Only one 100-notional ceil fits the continuous 160 gross. A wins the exact tie.
    assert result == {"A2501": 1}

    half = recover(
        floor_lots={},
        ideal_lots={"C2501": 0.5},
        lot_notionals={"C2501": 100.0},
        per_lot_margin={"C2501": 10.0},
        gross_budget=50.0,
        margin_budget=30.0,
        max_contract_volume=35,
    )
    assert half == {}


def test_max_contract_volume_is_never_exceeded():
    _, recover = _api()
    result = recover(
        floor_lots={"A2501": 35},
        ideal_lots={"A2501": 35.9},
        lot_notionals={"A2501": 100.0},
        per_lot_margin={"A2501": 10.0},
        gross_budget=3590.0,
        margin_budget=1000.0,
        max_contract_volume=35,
    )
    assert result == {"A2501": 35}


def test_research_integer_adapter_is_not_live_wired():
    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            assert "directional_survivor_integer_realization" not in path.read_text(encoding="utf-8")
