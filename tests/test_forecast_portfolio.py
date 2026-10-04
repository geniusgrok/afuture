import numpy as np
import pandas as pd
import pytest

from afuture.forecast_portfolio import forecast_covariance_portfolio


def tape(products=("A",), sigma=0.01):
    values = np.random.default_rng(17).normal(0, sigma, (63, len(products)))
    return pd.DataFrame(values, index=pd.bdate_range("2024-09-01", periods=63), columns=products)


def test_one_asset_matches_closed_form_after_cost_without_forced_full_allocation():
    returns = tape()
    variance = returns.A.var(ddof=1)
    expected = 0.1
    mu = 0.003 + expected * variance * (5 / 0.15)
    result = forecast_covariance_portfolio({"A": mu}, returns, decision_day="2025-01-02")
    assert result.ready
    assert result.weights["A"] == pytest.approx(expected)
    assert result.audit["risk_scale"] == 1


def test_no_net_edge_produces_zero_targets():
    result = forecast_covariance_portfolio({"A": -0.003}, tape(), decision_day="2025-01-02")
    assert result.ready and result.weights == {"A": 0.0}
    assert result.audit["status"] == "no_net_edge"


def test_gross_and_product_constraints_and_annual_risk_are_respected():
    products = tuple(chr(65 + i) for i in range(12))
    result = forecast_covariance_portfolio(
        dict.fromkeys(products, 0.1), tape(products, sigma=0.02), decision_day="2025-01-02"
    )
    assert result.ready
    assert sum(abs(w) for w in result.weights.values()) <= 2 + 1e-12
    assert max(abs(w) for w in result.weights.values()) <= 0.25 + 1e-12
    assert result.audit["post_scale_annual_risk"] <= 0.15 + 1e-12
    assert result.audit["pre_scale_utility"] >= 0


def test_future_returns_do_not_change_allocations_and_missing_prior_rows_close_targets():
    returns = tape()
    initial = forecast_covariance_portfolio({"A": 0.005}, returns, decision_day="2025-01-02")
    future = pd.DataFrame({"A": [np.inf]}, index=pd.to_datetime(["2025-01-02"]))
    changed = forecast_covariance_portfolio(
        {"A": 0.005}, pd.concat([returns, future]), decision_day="2025-01-02"
    )
    assert initial == changed
    returns.iloc[-1, 0] = np.nan
    invalid = forecast_covariance_portfolio({"A": 0.005}, returns, decision_day="2025-01-02")
    assert not invalid.ready and invalid.weights == {"A": 0}


def test_signed_short_targets_and_invalid_forecasts():
    result = forecast_covariance_portfolio({"A": -0.01}, tape(), decision_day="2025-01-02")
    assert result.weights["A"] < 0
    for value in (True, np.nan, np.inf):
        with pytest.raises(ValueError):
            forecast_covariance_portfolio({"A": value}, tape(), decision_day="2025-01-02")
