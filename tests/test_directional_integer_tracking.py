from pathlib import Path


def _api():
    from afuture.directional_integer_tracking import optimize_integer_tracking_target

    return optimize_integer_tracking_target


def test_uses_rounding_residual_without_exceeding_original_gross_or_margin_budget():
    optimize = _api()
    result = optimize(
        desired_lots={"A2501": 0.6, "M2501": 0.6},
        lot_notionals={"A2501": 100.0, "M2501": 100.0},
        per_lot_margin={"A2501": 10.0, "M2501": 10.0},
        target_gross_budget=120.0,
        margin_budget=12.0,
        max_contract_volume=35,
    )

    assert result == {"A2501": 1}
    assert sum(abs(v) * 100.0 for v in result.values()) <= 120.0
    assert sum(abs(v) * 10.0 for v in result.values()) <= 12.0


def test_tracking_error_breaks_equal_gross_ties_without_outcome_information():
    optimize = _api()
    result = optimize(
        desired_lots={"A2501": 1.6, "M2501": 0.4},
        lot_notionals={"A2501": 100.0, "M2501": 100.0},
        per_lot_margin={"A2501": 10.0, "M2501": 10.0},
        target_gross_budget=200.0,
        margin_budget=20.0,
        max_contract_volume=35,
    )

    assert result == {"A2501": 2}


def test_direction_is_preserved_and_contract_cannot_exceed_ceil_neighborhood():
    optimize = _api()
    result = optimize(
        desired_lots={"AG2606": -1.2, "M2605": 2.1},
        lot_notionals={"AG2606": 120.0, "M2605": 50.0},
        per_lot_margin={"AG2606": 22.5, "M2605": 9.375},
        target_gross_budget=260.0,
        margin_budget=50.0,
        max_contract_volume=35,
    )

    assert result["AG2606"] < 0
    assert result["M2605"] > 0
    assert abs(result["AG2606"]) <= 2
    assert abs(result["M2605"]) <= 3


def test_tight_margin_budget_can_reduce_below_floor_but_never_adds_new_symbols():
    optimize = _api()
    result = optimize(
        desired_lots={"A2501": 3.8, "M2501": 2.8},
        lot_notionals={"A2501": 100.0, "M2501": 100.0},
        per_lot_margin={"A2501": 20.0, "M2501": 20.0},
        target_gross_budget=600.0,
        margin_budget=60.0,
        max_contract_volume=35,
    )

    assert set(result) <= {"A2501", "M2501"}
    assert sum(abs(v) * 20.0 for v in result.values()) <= 60.0
    assert sum(abs(v) for v in result.values()) == 3


def test_max_contract_volume_remains_hard_cap():
    optimize = _api()
    result = optimize(
        desired_lots={"M2501": 80.0},
        lot_notionals={"M2501": 10.0},
        per_lot_margin={"M2501": 1.0},
        target_gross_budget=1000.0,
        margin_budget=1000.0,
        max_contract_volume=35,
    )

    assert result == {"M2501": 35}


def test_live_runtime_does_not_import_research_integer_tracking_adapter():
    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            assert "directional_integer_tracking" not in path.read_text(encoding="utf-8")
