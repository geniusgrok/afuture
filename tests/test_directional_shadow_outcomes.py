import pandas as pd


def test_build_shadow_outcomes_conditions_return_on_fixed_baseline_direction():
    from afuture.directional_shadow_outcomes import build_shadow_signal_outcomes

    dates = pd.to_datetime(["2026-01-05", "2026-01-06"])
    weights = pd.DataFrame({"AG": [1.0, -1.0], "CU": [0.0, 0.5]}, index=dates)
    intraday = pd.DataFrame({"AG": [0.02, 0.03], "CU": [0.01, -0.02]}, index=dates)

    ledger = build_shadow_signal_outcomes(weights, intraday)

    values = ledger.set_index(["date", "product"])["gross_return"]
    assert abs(values.loc[(pd.Timestamp("2026-01-05"), "AG")] - 0.02) < 1e-12
    assert abs(values.loc[(pd.Timestamp("2026-01-06"), "AG")] + 0.03) < 1e-12
    assert abs(values.loc[(pd.Timestamp("2026-01-06"), "CU")] + 0.02) < 1e-12
    assert (ledger["baseline_abs_weight"] > 0.0).all()


def test_decision_uses_only_completed_shadow_outcomes_strictly_before_day():
    from afuture.directional_shadow_outcomes import optimize_with_shadow_outcomes

    ledger = pd.DataFrame(
        [
            {"date": "2026-01-05", "product": "AG", "gross_return": 0.03, "baseline_abs_weight": 1.0},
            {"date": "2026-01-05", "product": "CU", "gross_return": -0.01, "baseline_abs_weight": 1.0},
            {"date": "2026-01-06", "product": "CU", "gross_return": 99.0, "baseline_abs_weight": 1.0},
        ]
    )
    kwargs = dict(
        decision_date=pd.Timestamp("2026-01-06"),
        reference_lots={"CU2606": 1},
        requested_lots={"AG2606": 1, "CU2606": 1},
        current_lots={"CU2606": 1},
        symbol_products={"AG2606": "AG", "CU2606": "CU"},
        lot_notionals={"AG2606": 10000.0, "CU2606": 10000.0},
        per_lot_margin={"AG2606": 2000.0, "CU2606": 2000.0},
        equity=10000.0,
        soft_margin_budget=2000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        cost_rate=0.0015,
    )

    result = optimize_with_shadow_outcomes(shadow_outcomes=ledger, **kwargs)

    assert result.completed_outcome_count == 2
    assert result.optimization.target_lots == {"AG2606": 1}


def test_shadow_objective_scales_expected_return_by_current_lot_notional():
    from afuture.directional_shadow_outcomes import optimize_with_shadow_outcomes

    ledger = pd.DataFrame(
        [
            {"date": "2026-01-05", "product": "AG", "gross_return": 0.02, "baseline_abs_weight": 1.0},
            {"date": "2026-01-05", "product": "CU", "gross_return": 0.01, "baseline_abs_weight": 1.0},
        ]
    )
    result = optimize_with_shadow_outcomes(
        shadow_outcomes=ledger,
        decision_date=pd.Timestamp("2026-01-06"),
        reference_lots={"CU2606": 1},
        requested_lots={"AG2606": 1, "CU2606": 1},
        current_lots={"CU2606": 1},
        symbol_products={"AG2606": "AG", "CU2606": "CU"},
        lot_notionals={"AG2606": 20000.0, "CU2606": 10000.0},
        per_lot_margin={"AG2606": 2000.0, "CU2606": 2000.0},
        equity=10000.0,
        soft_margin_budget=2000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        cost_rate=0.0015,
    )

    assert result.optimization.target_lots == {"AG2606": 1}
    assert result.product_expected_returns["AG"] > result.product_expected_returns["CU"]


def test_causal_remaining_horizon_scales_lifecycle_value_without_changing_daily_estimate():
    from afuture.directional_shadow_outcomes import optimize_with_shadow_outcomes

    ledger = pd.DataFrame(
        [
            {"date": "2026-01-05", "product": "AG", "gross_return": 0.006, "baseline_abs_weight": 1.0},
            {"date": "2026-01-05", "product": "CU", "gross_return": 0.010, "baseline_abs_weight": 1.0},
        ]
    )
    common = dict(
        shadow_outcomes=ledger,
        decision_date=pd.Timestamp("2026-01-06"),
        reference_lots={"CU2606": 1},
        requested_lots={"AG2606": 1, "CU2606": 1},
        current_lots={"CU2606": 1},
        symbol_products={"AG2606": "AG", "CU2606": "CU"},
        lot_notionals={"AG2606": 10000.0, "CU2606": 10000.0},
        per_lot_margin={"AG2606": 2000.0, "CU2606": 2000.0},
        equity=10000.0,
        soft_margin_budget=2000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        cost_rate=0.0015,
    )

    one_day = optimize_with_shadow_outcomes(**common)
    lifecycle = optimize_with_shadow_outcomes(
        **common,
        expected_horizons={"AG": 3.0, "CU": 1.0},
    )

    assert one_day.optimization.target_lots == {"CU2606": 1}
    assert lifecycle.optimization.target_lots == {"AG2606": 1}
    assert lifecycle.product_expected_returns == one_day.product_expected_returns
    assert lifecycle.product_expected_horizons == {"AG": 3.0, "CU": 1.0}


def test_no_shadow_outcomes_falls_back_exactly_to_reference():
    from afuture.directional_shadow_outcomes import optimize_with_shadow_outcomes

    result = optimize_with_shadow_outcomes(
        shadow_outcomes=pd.DataFrame(),
        decision_date=pd.Timestamp("2026-01-06"),
        reference_lots={"CU2606": 1},
        requested_lots={"AG2606": 1, "CU2606": 1},
        current_lots={"CU2606": 1},
        symbol_products={"AG2606": "AG", "CU2606": "CU"},
        lot_notionals={"AG2606": 10000.0, "CU2606": 10000.0},
        per_lot_margin={"AG2606": 2000.0, "CU2606": 2000.0},
        equity=10000.0,
        soft_margin_budget=2000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        cost_rate=0.0015,
    )

    assert result.optimization.target_lots == {"CU2606": 1}
    assert result.optimization.fallback_reason == "insufficient shadow outcome evidence"
