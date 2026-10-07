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


@pytest.mark.parametrize("risk_eligibility", ["all_assets", "per_asset"])
def test_gross_and_product_constraints_and_annual_risk_are_respected(risk_eligibility):
    products = tuple(chr(65 + i) for i in range(12))
    result = forecast_covariance_portfolio(
        dict.fromkeys(products, 0.1),
        tape(products, sigma=0.02),
        decision_day="2025-01-02",
        risk_eligibility=risk_eligibility,
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


@pytest.mark.parametrize(
    ("missing", "reason", "count"),
    [
        ("column", "missing_risk_product", 0),
        ("observation", "missing_completed_risk_observation", 62),
        ("variance", "zero_observed_risk", 63),
    ],
)
def test_per_asset_history_gate_keeps_usable_forecasts_and_audits_exclusions(
    missing, reason, count
):
    returns = tape(("A", "B"))
    if missing == "column":
        returns = returns.drop(columns="B")
    elif missing == "observation":
        returns.loc[returns.index[-1], "B"] = np.nan
    else:
        returns["B"] = 0.0
    reference = forecast_covariance_portfolio({"A": 0.004}, returns, decision_day="2025-01-02")
    legacy = forecast_covariance_portfolio(
        {"A": 0.004, "B": 0.9}, returns, decision_day="2025-01-02"
    )
    result = forecast_covariance_portfolio(
        {"A": 0.004, "B": 0.9},
        returns,
        decision_day="2025-01-02",
        risk_eligibility="per_asset",
    )
    assert not legacy.ready and legacy.weights == {"A": 0.0, "B": 0.0}
    assert result.ready and result.weights["A"] > 0
    assert result.weights["A"] == pytest.approx(reference.weights["A"])
    assert result.weights["B"] == 0.0
    assert result.audit["forecast_products"] == ["A", "B"]
    assert result.audit["included_products"] == result.audit["covariance_products"] == ["A"]
    assert result.audit["excluded_products"] == ["B"]
    assert result.audit["risk_history"]["A"] == {
        "observations": 63,
        "eligible": True,
        "included": True,
        "reason": "eligible",
    }
    assert result.audit["risk_history"]["B"] == {
        "observations": count,
        "eligible": False,
        "included": False,
        "reason": reason,
    }
    assert legacy.audit["included_products"] == []


def test_per_asset_future_returns_cannot_change_history_eligibility_or_allocation():
    returns = tape()
    forecasts = {"A": 0.004, "B": 0.009}
    initial = forecast_covariance_portfolio(
        forecasts, returns, decision_day="2025-01-02", risk_eligibility="per_asset"
    )
    future = pd.DataFrame(
        {"A": [np.inf, -1.0], "B": [0.01, 0.02]},
        index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
    )
    changed = forecast_covariance_portfolio(
        forecasts,
        pd.concat([returns, future]),
        decision_day="2025-01-02",
        risk_eligibility="per_asset",
    )
    assert changed == initial


def test_per_asset_missing_rows_are_not_backfilled_and_all_unavailable_closes():
    returns = tape()
    earlier = pd.DataFrame({"A": [0.002]}, index=[returns.index[0] - pd.Timedelta(days=1)])
    returns = pd.concat([earlier, returns])
    returns.loc[returns.index[-1], "A"] = np.nan
    result = forecast_covariance_portfolio(
        {"A": 0.1, "B": 0.1},
        returns,
        decision_day="2025-01-02",
        risk_eligibility="per_asset",
    )
    assert not result.ready and result.weights == {"A": 0.0, "B": 0.0}
    assert result.audit["status"] == "no_eligible_risk_assets"
    assert result.audit["observations"] == 63
    assert result.audit["risk_history"]["A"]["observations"] == 62
    assert result.audit["included_products"] == []
    assert result.audit["risk_window_first"] == returns.index[1].date().isoformat()


def test_per_asset_joint_covariance_and_cost_match_interior_solution():
    returns = tape(("A", "B"))
    returns["B"] = 0.6 * returns.A + 0.5 * returns.B
    covariance = returns.cov().to_numpy()
    covariance = 0.5 * covariance + 0.5 * np.diag(np.diag(covariance))
    desired = np.array([0.08, 0.06])
    forecasts = dict(zip(("A", "B"), 0.003 + (5 / 0.15) * covariance @ desired, strict=True))
    forecasts["C"] = 0.5
    result = forecast_covariance_portfolio(
        forecasts, returns, decision_day="2025-01-02", risk_eligibility="per_asset"
    )
    assert result.ready
    assert [result.weights["A"], result.weights["B"]] == pytest.approx(desired, abs=1e-8)
    assert result.weights["C"] == 0.0
    assert result.audit["covariance_products"] == ["A", "B"]
    expected_risk = np.sqrt(252 * desired @ covariance @ desired)
    assert result.audit["post_scale_annual_risk"] == pytest.approx(expected_risk, abs=1e-8)
    assert result.audit["pre_scale_utility"] > 0


@pytest.mark.parametrize("invalid", [np.inf, -1.0, -1.1])
def test_per_asset_invalid_risk_return_raises_instead_of_excluding(invalid):
    returns = tape(("A", "B"))
    returns.loc[returns.index[-1], "B"] = invalid
    with pytest.raises(ValueError, match="invalid market risk returns"):
        forecast_covariance_portfolio(
            {"A": 0.004, "B": 0.004},
            returns,
            decision_day="2025-01-02",
            risk_eligibility="per_asset",
        )


def test_per_asset_no_net_edge_is_ready_but_explicitly_zero():
    result = forecast_covariance_portfolio(
        {"A": -0.003, "B": 0.1},
        tape(),
        decision_day="2025-01-02",
        risk_eligibility="per_asset",
    )
    assert result.ready and result.weights == {"A": 0.0, "B": 0.0}
    assert result.audit["status"] == "no_net_edge"
    assert result.audit["included_products"] == ["A"]
