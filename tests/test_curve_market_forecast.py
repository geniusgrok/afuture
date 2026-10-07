from math import sqrt

import numpy as np
import pandas as pd
import pytest

from afuture.curve_market_forecast import forecast_from_mature_curve

PRODUCTS = tuple("A B C M Y P I J JM L V PP EG EB TA MA SA FG RB HC".split())
GROUPS = {p: "one" if i < 10 else "two" for i, p in enumerate(PRODUCTS)}


def samples():
    rows = []
    for day in pd.bdate_range("2023-01-02", periods=126):
        for i, product in enumerate(PRODUCTS):
            sign = 1 if i % 2 else -1
            rows.append(
                dict(
                    entry_day=day,
                    maturity_day=day + pd.offsets.BDay(5),
                    product=product,
                    symbol=product + "2501",
                    volatility=0.01,
                    curve_level=sign,
                    curve_change=0.0,
                    pressure=0.0,
                    gross_return=sign * 0.01 * sqrt(5),
                )
            )
    return pd.DataFrame(rows)


def predict(sample, *, grouped=False, relative=False, excluded_products=()):
    target = sample.iloc[:20].drop(columns=["maturity_day", "gross_return"]).copy()
    target["entry_day"] = pd.Timestamp("2025-01-02")
    return forecast_from_mature_curve(
        sample,
        target,
        decision_day="2025-01-02",
        grouped=grouped,
        relative=relative,
        groups=GROUPS,
        excluded_products=excluded_products,
    )


def test_shared_curve_forecast_matches_analytic_ridge_solution():
    result = predict(samples())
    assert result.ready
    assert result.audit["coefficients"]["shared"]["curve_level"] == pytest.approx(126 / 127)
    assert result.forecasts.expected_return.iloc[0] == pytest.approx(-126 / 127 * 0.01 * sqrt(5))


def test_group_deviations_can_learn_opposite_economic_responses():
    sample = samples()
    sample.loc[sample["product"].map(GROUPS).eq("two"), "gross_return"] *= -1
    shared, grouped = predict(sample), predict(sample, grouped=True)
    assert abs(shared.forecasts.expected_return.max()) < 1e-12
    assert grouped.forecasts.expected_return.iloc[0] < 0
    assert grouped.forecasts.expected_return.iloc[10] > 0
    assert grouped.audit["group_deviation_penalty"] == 20


def test_relative_labels_ignore_common_group_return_not_price_information():
    sample = samples()
    initial = predict(sample, grouped=True, relative=True)
    sample["gross_return"] += sample["product"].map(GROUPS).map({"one": 0.2, "two": -0.1})
    changed = predict(sample, grouped=True, relative=True)
    np.testing.assert_allclose(
        changed.forecasts.expected_return, initial.forecasts.expected_return, atol=1e-12
    )


def test_future_labels_cannot_change_curve_fit():
    sample = samples()
    future = sample.iloc[:20].copy()
    future["entry_day"], future["maturity_day"] = (
        pd.Timestamp("2024-12-30"),
        pd.Timestamp("2025-01-02"),
    )
    future["gross_return"], future["curve_level"] = np.nan, np.nan
    pd.testing.assert_frame_equal(
        predict(pd.concat([sample, future])).forecasts, predict(sample).forecasts
    )


def test_exclusions_are_independent_refits_before_numeric_reads():
    sample = samples()
    sample.loc[sample["product"].eq("FG"), ["gross_return", "volatility"]] = np.nan
    result = predict(sample, excluded_products=("FG",))
    # 19products fails the declared breadth gate, rather than fabricating evidence.
    assert not result.ready and result.forecasts.empty
    with pytest.raises(ValueError, match="non-finite"):
        predict(sample)


def test_unknown_groups_and_noncanonical_exclusions_fail_closed():
    sample = samples()
    with pytest.raises(ValueError):
        predict(sample, excluded_products="FG")
    with pytest.raises(ValueError):
        forecast_from_mature_curve(
            sample,
            sample.iloc[:20].assign(entry_day="2025-01-02"),
            decision_day="2025-01-02",
            grouped=True,
            groups={},
        )


def role_samples():
    sample = samples()
    sample["symbol_near"] = sample["product"] + "2501"
    sample["symbol_far"] = sample["product"] + "2505"
    near = sample["product"].isin(PRODUCTS[:10])
    sample["symbol"] = np.where(near, sample.symbol_near, sample.symbol_far)
    sample["curve_level"] = 1.0
    sample["gross_return"] = np.where(near, 0.02, -0.02)
    sample["feature_through"] = sample.entry_day - pd.offsets.BDay(1)
    sample["pair_source_day"] = sample.feature_through
    target = sample.iloc[:20].drop(columns=["gross_return", "maturity_day"]).copy()
    target["entry_day"] = pd.Timestamp("2025-01-02")
    target["feature_through"] = target["pair_source_day"] = pd.Timestamp("2024-12-31")
    return sample, target


def test_contract_role_learns_opposite_responses_without_imposed_trade_direction():
    sample, target = role_samples()
    baseline = forecast_from_mature_curve(sample, target, decision_day="2025-01-02")
    result = forecast_from_mature_curve(
        sample, target, decision_day="2025-01-02", contract_role=True
    )
    assert baseline.forecasts.expected_return.abs().max() < 1e-12
    near = result.forecasts["product"].isin(PRODUCTS[:10])
    assert result.forecasts.loc[near, "expected_return"].min() > 0.01
    assert result.forecasts.loc[~near, "expected_return"].max() < -0.01


def test_role_requires_prior_exact_pair_and_ignores_unavailable_labels():
    sample, target = role_samples()
    kwargs = dict(decision_day="2025-01-02", contract_role=True)
    original = forecast_from_mature_curve(sample, target, **kwargs)
    future = sample.iloc[:20].assign(
        entry_day=pd.Timestamp("2024-12-26"),
        maturity_day=pd.Timestamp("2025-01-02"),
        gross_return=np.nan,
        symbol_near="invalid",
        symbol_far="invalid",
    )
    changed = forecast_from_mature_curve(pd.concat([sample, future]), target, **kwargs)
    pd.testing.assert_frame_equal(original.forecasts, changed.forecasts)
    for corrupted in (
        target.assign(symbol_far=target.symbol_near),
        target.assign(pair_source_day=target.entry_day),
    ):
        with pytest.raises(ValueError, match="ambiguous or not strictly prior"):
            forecast_from_mature_curve(sample, corrupted, **kwargs)
