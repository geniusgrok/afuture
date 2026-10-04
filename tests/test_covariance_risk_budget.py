import numpy as np
import pandas as pd
import pytest

from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_risk import covariance_risk_budget
from afuture.directional_robustness import CovarianceBudgetDirectionalProductionAcceptance


def _returns():
    index = pd.bdate_range(end="2026-08-20", periods=63)
    values = np.resize(np.array([-0.03, 0.03]), 63)
    return pd.DataFrame({"A": values, "M": values}, index=index)


def test_forecast_caps_target_volatility_without_gross_expansion():
    market = _returns()
    forecast = float(market.A.std(ddof=1) * np.sqrt(252))
    decision = covariance_risk_budget({"A": -1.0}, market)

    assert decision.forecast_annualized_volatility == pytest.approx(forecast)
    assert decision.scale == pytest.approx(0.15 / forecast)
    assert decision.reason == "risk_budget_scaled"
    boundary = covariance_risk_budget({"A": 1.0}, market, target_annualized_volatility=forecast)
    assert boundary.scale == 1.0
    assert covariance_risk_budget({"A": 0.1}, market).scale == 1.0
    assert covariance_risk_budget({"A": 1.0}, market * 0.0).scale == 1.0


def test_shrinkage_preserves_correlation_information_without_free_hedge_leverage():
    market = _returns()
    matched = covariance_risk_budget({"A": 0.5, "M": -0.5}, market)
    common = covariance_risk_budget({"A": 0.5, "M": 0.5}, market)
    single_risk = float(market.A.std(ddof=1) * np.sqrt(252))

    assert matched.forecast_annualized_volatility == pytest.approx(0.5 * single_risk)
    assert common.forecast_annualized_volatility == pytest.approx(np.sqrt(0.75) * single_risk)
    assert 0.0 < matched.scale <= 1.0
    assert common.scale < matched.scale


def test_missing_inputs_prohibit_targets_without_filling_or_extending_window():
    market = _returns()
    short = covariance_risk_budget({"A": 1.0}, market.iloc[1:])
    assert short.scale == 0.0
    assert short.reason == "insufficient_completed_history"
    assert short.observation_count == 62
    absent = covariance_risk_budget({"CU": 1.0}, market)
    assert absent.scale == 0.0
    assert absent.reason == "missing_active_products:CU"
    market.loc[pd.Timestamp("2026-08-21"), "A"] = np.nan
    missing = covariance_risk_budget({"A": 1.0}, market)
    assert missing.scale == 0.0
    assert missing.reason == "missing_active_observations"
    assert missing.observation_count == 63
    assert covariance_risk_budget({"A": 0.0}, market).reason == "no_active_targets"


@pytest.mark.parametrize("value", [float("inf"), -float("inf"), -1.0, -1.01])
def test_invalid_active_return_evidence_fails_closed(value):
    market = _returns()
    market.iloc[-1, 0] = value
    with pytest.raises(ValueError, match="greater than -100%"):
        covariance_risk_budget({"A": 1.0}, market)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -float("inf")])
def test_nonfinite_weights_are_rejected(value):
    with pytest.raises(ValueError, match="finite values"):
        covariance_risk_budget({"A": value}, _returns())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_annualized_volatility": -0.1},
        {"target_annualized_volatility": float("nan")},
        {"target_annualized_volatility": float("inf")},
        {"annualization": 0},
        {"annualization": 252.5},
        {"lookback_days": 1},
        {"lookback_days": 63.5},
    ],
)
def test_invalid_risk_budget_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        covariance_risk_budget({"A": 1.0}, _returns(), **kwargs)


def test_forecasts_exclude_current_and_future_sessions_and_ignore_account_returns():
    prior = _returns()
    extended = prior.copy()
    extended.loc[pd.Timestamp("2026-08-21")] = [float("inf"), -1.0]
    extended.loc[pd.Timestamp("2026-08-24")] = [-1.1, float("nan")]
    first = CovarianceBudgetDirectionalProductionAcceptance(market_returns=prior)
    second = CovarianceBudgetDirectionalProductionAcceptance(market_returns=extended)
    for account in (first, second):
        account.observe_target_state(day=pd.Timestamp("2026-08-21"), product_weights={"A": 1.0})
    assert first.risk_audit == second.risk_audit
    assert second.risk_audit[0]["prior_through"] == pd.Timestamp("2026-08-20")
    assert second.risk_governor.scale([-0.5, 0.5]) == first.risk_governor.scale([0.0])


@pytest.mark.parametrize("kind", ["duplicate_day", "unsorted", "duplicate_product"])
def test_ambiguous_market_evidence_is_rejected(kind):
    market = _returns()
    if kind == "duplicate_day":
        market = pd.concat([market, market.iloc[-1:]])
    elif kind == "unsorted":
        market = market.iloc[::-1]
    else:
        market.columns = ["A", "a"]
    with pytest.raises(ValueError):
        CovarianceBudgetDirectionalProductionAcceptance(market_returns=market)


def _contracts():
    dates = pd.to_datetime(["2026-08-20", "2026-08-21", "2026-08-24"])
    return pd.DataFrame(
        {
            "date": dates,
            "product": "A",
            "symbol": "A2612",
            "delivery": pd.Timestamp("2026-12-15"),
            "open": [1000.0, 1000.0, 1020.0],
            "close": [1000.0, 1010.0, 1020.0],
            "volume": 5000,
            "hold": 30000,
        }
    )


def test_real_simulator_applies_forecast_then_reduces_when_risk_evidence_goes_missing():
    market = _returns()
    market.loc[pd.Timestamp("2026-08-21"), "A"] = np.nan
    account = CovarianceBudgetDirectionalProductionAcceptance(
        ProductionMechanicsConfig(initial_capital=100000.0), market_returns=market
    )
    weights = pd.DataFrame({"A": [1.0, 1.0]}, index=pd.to_datetime(["2026-08-21", "2026-08-24"]))
    result = account.simulate(_contracts(), weights, cost_bps=5.0)

    assert 0.0 < result.daily.risk_scale.iloc[0] < 1.0
    assert result.daily.risk_scale.iloc[1] == 0.0
    assert account.risk_audit[1]["reason"] == "missing_active_observations"
    trades = result.events.loc[result.events.kind == "trade"]
    assert list(trades.delta_lots) == [3, -3]
    assert list(trades.price) == [1000.0, 1020.0]
    assert list(trades.transaction_cost) == pytest.approx([15.0, 15.3])
    pnl = result.events.loc[result.events.kind == "pnl", "gross_pnl"].sum()
    assert pnl == pytest.approx(600.0)
    assert result.final_equity == pytest.approx(100000.0 + 600.0 - 30.3)
    assert result.daily.gross_notional.iloc[-1] == 0.0
    assert not result.daily.halted.any()
    assert (
        account.account_risk_reason(
            equity=69000.0, day_start_equity=100000.0, high_watermark=100000.0
        )
        == "drawdown limit reached"
    )


def test_empty_market_history_is_explicitly_unavailable():
    account = CovarianceBudgetDirectionalProductionAcceptance(market_returns=pd.DataFrame())
    account.observe_target_state(day=pd.Timestamp("2026-08-21"), product_weights={"A": 1.0})
    assert account.risk_governor.scale([]) == 0.0
    assert account.risk_audit[0]["prior_through"] is None
    assert account.risk_audit[0]["reason"] == "missing_active_products:A"
