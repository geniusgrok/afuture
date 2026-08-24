import pytest


def _api():
    from afuture.directional_net_edge_allocator import optimize_net_edge_targets

    return optimize_net_edge_targets


def test_weak_positive_gross_edge_is_rejected_when_entry_cost_is_larger():
    optimize = _api()
    result = optimize(
        reference_lots={},
        requested_lots={"AG2612": 1},
        current_lots={},
        symbol_products={"AG2612": "AG"},
        lot_notionals={"AG2612": 100_000.0},
        per_lot_margin={"AG2612": 10_000.0},
        equity=500_000.0,
        soft_margin_budget=150_000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        expected_product_returns={"AG": 0.0010},
        cost_rate=0.0015,
    )
    assert result.optimization.target_lots == {}
    assert result.expected_gross_alpha == 0.0
    assert result.transition_cost == 0.0


def test_higher_value_product_can_replace_lower_value_lot_at_capacity():
    optimize = _api()
    result = optimize(
        reference_lots={"CU2610": 1},
        requested_lots={"CU2610": 1, "AG2612": 1},
        current_lots={"CU2610": 1},
        symbol_products={"CU2610": "CU", "AG2612": "AG"},
        lot_notionals={"CU2610": 1_000.0, "AG2612": 1_000.0},
        per_lot_margin={"CU2610": 300.0, "AG2612": 300.0},
        equity=1_000.0,
        soft_margin_budget=350.0,
        max_gross_ratio=1.0,
        max_abs_lots=35,
        expected_product_returns={"CU": 0.01, "AG": 0.03},
        cost_rate=0.0015,
    )
    assert result.optimization.target_lots == {"AG2612": 1}
    assert result.optimization.improvement > 0.0
    assert result.expected_gross_alpha == pytest.approx(30.0)
    assert result.transition_cost == pytest.approx(3.0)


def test_recovered_product_can_regain_capital_without_candidate_owned_history():
    optimize = _api()
    result = optimize(
        reference_lots={},
        requested_lots={"AG2612": 2},
        current_lots={},
        symbol_products={"AG2612": "AG"},
        lot_notionals={"AG2612": 50_000.0},
        per_lot_margin={"AG2612": 7_500.0},
        equity=500_000.0,
        soft_margin_budget=150_000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        expected_product_returns={"AG": 0.02},
        cost_rate=0.0015,
    )
    assert result.optimization.target_lots == {"AG2612": 2}
    assert result.expected_gross_alpha == pytest.approx(2_000.0)
    assert result.transition_cost == pytest.approx(150.0)


def test_allocator_preserves_sign_intent_and_hard_35_lot_cap():
    optimize = _api()
    result = optimize(
        reference_lots={},
        requested_lots={"AG2612": -40},
        current_lots={},
        symbol_products={"AG2612": "AG"},
        lot_notionals={"AG2612": 1.0},
        per_lot_margin={"AG2612": 1.0},
        equity=1_000_000_000.0,
        soft_margin_budget=350_000_000.0,
        max_gross_ratio=2.0,
        max_abs_lots=100,
        expected_product_returns={"AG": 1.0},
        cost_rate=0.0,
    )
    assert result.optimization.target_lots == {"AG2612": -35}


def test_unchanged_target_has_zero_transition_cost():
    optimize = _api()
    result = optimize(
        reference_lots={"CU2610": 2},
        requested_lots={"CU2610": 2},
        current_lots={"CU2610": 2},
        symbol_products={"CU2610": "CU"},
        lot_notionals={"CU2610": 10_000.0},
        per_lot_margin={"CU2610": 1_500.0},
        equity=500_000.0,
        soft_margin_budget=150_000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        expected_product_returns={"CU": 0.01},
        cost_rate=0.0015,
    )
    assert result.optimization.target_lots == {"CU2610": 2}
    assert result.transition_cost == 0.0


def test_allocator_rejects_soft_margin_budget_above_hard_gate():
    optimize = _api()
    with pytest.raises(ValueError, match="35%"):
        optimize(
            reference_lots={},
            requested_lots={"AG2612": 1},
            current_lots={},
            symbol_products={"AG2612": "AG"},
            lot_notionals={"AG2612": 10_000.0},
            per_lot_margin={"AG2612": 1_500.0},
            equity=100_000.0,
            soft_margin_budget=36_000.0,
            max_gross_ratio=2.0,
            max_abs_lots=35,
            expected_product_returns={"AG": 0.01},
            cost_rate=0.0015,
        )
