from afuture.directional_efficiency import stabilize_product_replacements


def test_low_edge_product_replacement_keeps_incumbent_without_increasing_gross():
    previous = {"A": 1.0, "M": -1.0}
    candidate = {"RB": 1.0, "M": -1.0}

    result = stabilize_product_replacements(
        previous,
        candidate,
        trailing_mean_returns={"A": 0.0020, "RB": 0.0023, "M": -0.0010},
        horizon=3,
        cost_bps=15.0,
    )

    assert result == previous
    assert sum(abs(value) for value in result.values()) <= sum(
        abs(value) for value in candidate.values()
    )


def test_multiple_low_edge_replacements_are_evaluated_as_one_portfolio_transition():
    previous = {"A": 0.75, "M": -0.75, "CU": 0.50}
    candidate = {"RB": 0.75, "M": -0.75, "AG": 0.50}

    result = stabilize_product_replacements(
        previous,
        candidate,
        trailing_mean_returns={
            "A": 0.0015,
            "RB": 0.0018,
            "M": -0.0010,
            "CU": 0.0010,
            "AG": 0.0012,
        },
        horizon=3,
        cost_bps=15.0,
    )

    assert result == previous


def test_high_edge_product_replacement_pays_switch_cost_and_proceeds():
    previous = {"A": 1.0, "M": -1.0}
    candidate = {"RB": 1.0, "M": -1.0}

    result = stabilize_product_replacements(
        previous,
        candidate,
        trailing_mean_returns={"A": 0.0010, "RB": 0.0040, "M": -0.0010},
        horizon=3,
        cost_bps=15.0,
    )

    assert result == candidate


def test_product_replacement_never_delays_sign_reversal_or_risk_reduction():
    assert stabilize_product_replacements(
        {"A": 1.0, "M": -1.0},
        {"A": -1.0, "M": -1.0},
        trailing_mean_returns={"A": 0.01, "M": -0.01},
        horizon=3,
        cost_bps=15.0,
    ) == {"A": -1.0, "M": -1.0}

    assert stabilize_product_replacements(
        {"A": 1.0, "M": -1.0},
        {"M": -1.0},
        trailing_mean_returns={"A": 0.01, "M": -0.01},
        horizon=3,
        cost_bps=15.0,
    ) == {"M": -1.0}
