import pandas as pd


def _api():
    from afuture.directional_cost_aware_no_trade import (
        BENEFIT_HORIZON_SESSIONS,
        COST_HURDLE_BPS,
        TREND_LOOKBACK_SESSIONS,
        apply_cost_aware_no_trade,
    )

    return (
        TREND_LOOKBACK_SESSIONS,
        BENEFIT_HORIZON_SESSIONS,
        COST_HURDLE_BPS,
        apply_cost_aware_no_trade,
    )


def _prices(values):
    index = pd.bdate_range("2026-01-01", periods=len(values))
    return pd.DataFrame({"AG": values}, index=index)


def test_constants_are_the_predeclared_pr17_candidate():
    lookback, horizon, hurdle, _ = _api()
    assert lookback == 20
    assert horizon == 3
    assert hurdle == 15.0


def test_weak_new_entry_is_suppressed_when_three_session_benefit_does_not_pay_15bp():
    _, _, _, apply_filter = _api()
    prices = _prices([100.0] * 22)
    weights = pd.DataFrame(0.0, index=prices.index, columns=["AG"])
    weights.iloc[-1, 0] = 2.0

    result = apply_filter(weights=weights, close_prices=prices)

    assert result.iloc[-1, 0] == 0.0


def test_strong_completed_trend_allows_new_entry():
    _, _, _, apply_filter = _api()
    prices = _prices([100.0 + i * 0.2 for i in range(22)])
    weights = pd.DataFrame(0.0, index=prices.index, columns=["AG"])
    weights.iloc[-1, 0] = 2.0

    result = apply_filter(weights=weights, close_prices=prices)

    assert result.iloc[-1, 0] == 2.0


def test_archived_trend_uses_sum_of_completed_daily_returns_not_compounded_return():
    _, _, _, apply_filter = _api()
    daily_returns = [0.10, -0.095] * 10
    values = [100.0]
    for daily_return in daily_returns:
        values.append(values[-1] * (1.0 + daily_return))
    values.append(values[-1])
    prices = _prices(values)
    weights = pd.DataFrame(0.0, index=prices.index, columns=["AG"])
    weights.iloc[-1, 0] = 2.0

    result = apply_filter(weights=weights, close_prices=prices)

    # The arithmetic sum is +5%, so the archived 3/20 benefit is +75bp and clears
    # the 15bp hurdle. The compounded 20-session return is negative; using it would
    # incorrectly suppress this entry and break the archived 440.8444x lineage.
    assert result.iloc[-1, 0] == 2.0


def test_same_sign_increase_can_be_suppressed_but_reduction_is_never_delayed():
    _, _, _, apply_filter = _api()
    prices = _prices([100.0] * 24)
    weights = pd.DataFrame(1.0, index=prices.index, columns=["AG"])
    weights.iloc[-2, 0] = 2.0
    weights.iloc[-1, 0] = 0.5

    result = apply_filter(weights=weights, close_prices=prices, initial_weights={"AG": 1.0})

    assert result.iloc[-3, 0] == 1.0
    assert result.iloc[-2, 0] == 1.0
    assert result.iloc[-1, 0] == 0.5


def test_reversal_bypasses_filter_even_when_completed_trend_disagrees():
    _, _, _, apply_filter = _api()
    prices = _prices([100.0] * 22)
    weights = pd.DataFrame(1.0, index=prices.index, columns=["AG"])
    weights.iloc[-1, 0] = -2.0

    result = apply_filter(weights=weights, close_prices=prices, initial_weights={"AG": 1.0})

    assert result.iloc[-1, 0] == -2.0


def test_decision_uses_only_close_history_completed_before_target_session():
    _, _, _, apply_filter = _api()
    prices = _prices([100.0] * 21 + [140.0])
    weights = pd.DataFrame(0.0, index=prices.index, columns=["AG"])
    weights.iloc[-1, 0] = 2.0

    baseline = apply_filter(weights=weights, close_prices=prices)
    changed = prices.copy()
    changed.iloc[-1, 0] = 1000.0
    same_decision = apply_filter(weights=weights, close_prices=changed)

    assert baseline.iloc[-1, 0] == same_decision.iloc[-1, 0] == 0.0


def test_filter_never_exceeds_raw_two_x_gross():
    _, _, _, apply_filter = _api()
    index = pd.bdate_range("2026-01-01", periods=22)
    prices = pd.DataFrame(
        {"AG": [100.0 + i for i in range(22)], "CU": [100.0 + i for i in range(22)]},
        index=index,
    )
    weights = pd.DataFrame(0.0, index=index, columns=["AG", "CU"])
    weights.iloc[-1] = [1.0, 1.0]

    result = apply_filter(weights=weights, close_prices=prices)

    assert float(result.abs().sum(axis=1).max()) <= 2.0 + 1e-12
