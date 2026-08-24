import math

from afuture.directional_integer_optimizer import optimize_integer_targets


def optimize(**overrides):
    values = dict(
        reference_lots={"AG2612": 1, "CU2610": 1},
        requested_lots={"AG2612": 2, "CU2610": 2},
        current_lots={"AG2612": 1, "CU2610": 1},
        lot_notionals={"AG2612": 100000.0, "CU2610": 100000.0},
        per_lot_margin={"AG2612": 15000.0, "CU2610": 15000.0},
        equity=500000.0,
        soft_margin_budget=45000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        objective=lambda lots: lots.get("AG2612", 0) * 200.0 + lots.get("CU2610", 0) * 100.0,
    )
    values.update(overrides)
    return optimize_integer_targets(**values)


def test_optimizer_moves_one_lot_to_strictly_higher_production_value():
    result = optimize()
    assert result.target_lots == {"AG2612": 2, "CU2610": 1}


def test_optimizer_never_creates_sign_absent_from_raw_request():
    result = optimize(
        reference_lots={}, requested_lots={"AG2612": 0}, current_lots={},
        lot_notionals={"AG2612": 100000.0}, per_lot_margin={"AG2612": 15000.0},
        objective=lambda lots: 1000.0 * abs(lots.get("AG2612", 0)),
    )
    assert result.target_lots == {}


def test_optimizer_never_exceeds_requested_magnitude():
    result = optimize(
        reference_lots={"AG2612": 1}, requested_lots={"AG2612": 1}, current_lots={"AG2612": 1},
        lot_notionals={"AG2612": 100000.0}, per_lot_margin={"AG2612": 15000.0},
        objective=lambda lots: 1000.0 * lots.get("AG2612", 0),
    )
    assert result.target_lots == {"AG2612": 1}


def test_optimizer_never_exceeds_35_lots():
    result = optimize(
        reference_lots={"AG2612": 35}, requested_lots={"AG2612": 40}, current_lots={"AG2612": 35},
        lot_notionals={"AG2612": 1000.0}, per_lot_margin={"AG2612": 100.0}, soft_margin_budget=100000.0,
        objective=lambda lots: 1000.0 * lots.get("AG2612", 0),
    )
    assert result.target_lots == {"AG2612": 35}


def test_optimizer_never_exceeds_two_x_gross():
    result = optimize(
        reference_lots={"AG2612": 9}, requested_lots={"AG2612": 20}, current_lots={"AG2612": 9},
        lot_notionals={"AG2612": 100000.0}, per_lot_margin={"AG2612": 1000.0}, equity=500000.0,
        soft_margin_budget=100000.0, objective=lambda lots: 1000.0 * lots.get("AG2612", 0),
    )
    assert result.target_lots == {"AG2612": 10}


def test_optimizer_never_exceeds_supplied_soft_margin_budget():
    result = optimize(
        reference_lots={"AG2612": 2}, requested_lots={"AG2612": 10}, current_lots={"AG2612": 2},
        lot_notionals={"AG2612": 10000.0}, per_lot_margin={"AG2612": 15000.0}, soft_margin_budget=45000.0,
        objective=lambda lots: 1000.0 * lots.get("AG2612", 0),
    )
    assert result.target_lots == {"AG2612": 3}


def test_optimizer_is_deterministic_on_equal_value_moves():
    result = optimize(
        reference_lots={}, requested_lots={"AG2612": 1, "CU2610": 1}, current_lots={},
        lot_notionals={"AG2612": 100000.0, "CU2610": 100000.0},
        per_lot_margin={"AG2612": 15000.0, "CU2610": 15000.0}, soft_margin_budget=15000.0,
        objective=lambda lots: 100.0 * sum(abs(v) for v in lots.values()),
    )
    assert result.target_lots == {"AG2612": 1}


def test_invalid_objective_falls_back_exactly_to_reference_target():
    reference = {"AG2612": 1, "CU2610": -1}
    result = optimize(
        reference_lots=reference, requested_lots={"AG2612": 2, "CU2610": -2}, current_lots=reference,
        objective=lambda lots: math.nan,
    )
    assert result.target_lots == reference
    assert result.fallback_reason == "non-finite objective"


def test_optimizer_can_reduce_below_raw_request_when_net_value_improves():
    result = optimize(
        reference_lots={"AG2612": 2}, requested_lots={"AG2612": 3}, current_lots={"AG2612": 2},
        lot_notionals={"AG2612": 100000.0}, per_lot_margin={"AG2612": 15000.0},
        objective=lambda lots: -100.0 * abs(lots.get("AG2612", 0)),
    )
    assert result.target_lots == {}


def test_optimizer_swaps_low_value_lot_for_higher_value_lot_when_capacity_is_full():
    result = optimize(
        reference_lots={"CU2610": 2}, requested_lots={"AG2612": 2, "CU2610": 2}, current_lots={"CU2610": 2},
        lot_notionals={"AG2612": 100000.0, "CU2610": 100000.0},
        per_lot_margin={"AG2612": 20000.0, "CU2610": 20000.0}, equity=500000.0,
        soft_margin_budget=40000.0,
        objective=lambda lots: lots.get("AG2612", 0) * 300.0 + lots.get("CU2610", 0) * 100.0,
    )
    assert result.target_lots == {"AG2612": 2}
