import pandas as pd


def test_survivor_reallocation_restores_original_gross_without_new_products():
    from afuture.directional_cost_survivor_reallocation import (
        renormalize_survivor_risk_budget,
    )

    index = pd.to_datetime(["2026-01-05"])
    raw = pd.DataFrame({"A": [1.0], "M": [-1.0], "Y": [0.0]}, index=index)
    filtered = pd.DataFrame({"A": [0.5], "M": [0.0], "Y": [0.0]}, index=index)

    result = renormalize_survivor_risk_budget(
        original_weights=raw,
        approved_weights=filtered,
    )

    assert result.loc[index[0], "A"] == 2.0
    assert result.loc[index[0], "M"] == 0.0
    assert result.loc[index[0], "Y"] == 0.0
    assert abs(float(result.abs().sum(axis=1).iloc[0]) - 2.0) < 1e-12


def test_survivor_reallocation_preserves_relative_survivor_weights_and_signs():
    from afuture.directional_cost_survivor_reallocation import (
        renormalize_survivor_risk_budget,
    )

    index = pd.to_datetime(["2026-01-05"])
    raw = pd.DataFrame({"A": [0.8], "M": [-0.8], "Y": [0.4]}, index=index)
    filtered = pd.DataFrame({"A": [0.4], "M": [-0.2], "Y": [0.0]}, index=index)

    result = renormalize_survivor_risk_budget(
        original_weights=raw,
        approved_weights=filtered,
    )

    assert result.loc[index[0], "A"] > 0.0
    assert result.loc[index[0], "M"] < 0.0
    assert result.loc[index[0], "Y"] == 0.0
    assert abs(abs(result.loc[index[0], "A"] / result.loc[index[0], "M"]) - 2.0) < 1e-12
    assert abs(float(result.abs().sum(axis=1).iloc[0]) - 2.0) < 1e-12


def test_survivor_reallocation_keeps_zero_when_nothing_clears_cost_gate():
    from afuture.directional_cost_survivor_reallocation import (
        renormalize_survivor_risk_budget,
    )

    index = pd.to_datetime(["2026-01-05"])
    raw = pd.DataFrame({"A": [1.0], "M": [-1.0]}, index=index)
    filtered = pd.DataFrame({"A": [0.0], "M": [0.0]}, index=index)

    result = renormalize_survivor_risk_budget(
        original_weights=raw,
        approved_weights=filtered,
    )

    assert float(result.abs().sum().sum()) == 0.0


def test_survivor_reallocation_rejects_new_support_or_excess_filtered_gross():
    import pytest
    from afuture.directional_cost_survivor_reallocation import (
        renormalize_survivor_risk_budget,
    )

    index = pd.to_datetime(["2026-01-05"])
    raw = pd.DataFrame({"A": [1.0], "M": [0.0]}, index=index)

    with pytest.raises(ValueError, match="new product support"):
        renormalize_survivor_risk_budget(
            original_weights=raw,
            approved_weights=pd.DataFrame({"A": [0.5], "M": [0.5]}, index=index),
        )

    with pytest.raises(ValueError, match="exceeds original gross"):
        renormalize_survivor_risk_budget(
            original_weights=raw,
            approved_weights=pd.DataFrame({"A": [1.1], "M": [0.0]}, index=index),
        )


def test_survivor_reallocation_never_exceeds_two_x():
    from afuture.directional_cost_survivor_reallocation import (
        renormalize_survivor_risk_budget,
    )

    index = pd.to_datetime(["2026-01-05"])
    raw = pd.DataFrame({"A": [0.6], "M": [-0.4]}, index=index)
    filtered = pd.DataFrame({"A": [0.3], "M": [-0.2]}, index=index)

    result = renormalize_survivor_risk_budget(
        original_weights=raw,
        approved_weights=filtered,
    )

    assert abs(float(result.abs().sum(axis=1).iloc[0]) - 1.0) < 1e-12
    assert float(result.abs().sum(axis=1).max()) <= 2.0 + 1e-12
