from math import sqrt

import numpy as np
import pandas as pd
import pytest

from afuture.curve_market_forecast import forecast_from_mature_curve
from afuture.curve_rebuild import ANNUAL_PERIOD_DAYS, forecast_curve_rebuild

PRODUCTS = tuple("A B C M Y P I J JM L V PP EG EB TA MA SA FG RB HC CU AL".split())
GROUPS = {product: "one" if i < 11 else "two" for i, product in enumerate(PRODUCTS)}


def samples():
    days = pd.bdate_range("2022-01-03", periods=504)
    frame = pd.DataFrame(
        [(day, product) for day in days for product in PRODUCTS], columns=["entry_day", "product"]
    )
    frame["maturity_day"] = frame.entry_day + pd.offsets.BDay(5)
    frame["feature_through"] = frame.entry_day - pd.offsets.BDay(1)
    frame["symbol"] = frame["product"] + "2501"
    frame["volatility"] = 0.01
    frame["curve_level"] = 1.0
    frame["curve_change"] = 0.0
    frame["pressure"] = 0.0
    elapsed = (frame.entry_day - pd.Timestamp("2000-01-01")).dt.days
    frame["gross_return"] = np.cos(2 * np.pi * elapsed / ANNUAL_PERIOD_DAYS) * 0.01 * sqrt(5)
    return frame


def predict(sample, *, day="2025-01-02", **kwargs):
    target = sample.iloc[: len(PRODUCTS)].drop(columns=["maturity_day", "gross_return"]).copy()
    target["entry_day"] = pd.Timestamp(day)
    target["feature_through"] = target.entry_day - pd.offsets.BDay(1)
    return forecast_curve_rebuild(sample, target, decision_day=day, **kwargs)


def test_independent_refit_matches_existing_model_after_training_exclusions():
    sample = samples()
    target = sample.iloc[:22].assign(entry_day=pd.Timestamp("2025-01-02"))
    original = forecast_from_mature_curve(
        sample, target, decision_day="2025-01-02", excluded_products=("FG", "JM")
    )
    rebuilt = predict(sample, excluded_products=("FG", "JM"))
    pd.testing.assert_frame_equal(rebuilt.forecasts, original.forecasts)
    assert rebuilt.audit["training_products"] == 20
    assert not rebuilt.forecasts["product"].isin(["FG", "JM"]).any()


def test_annual_curve_response_learns_opposite_seasonal_premia():
    sample = samples()
    january = predict(sample, seasonal=True)
    july = predict(sample, seasonal=True, day="2025-07-02")
    assert january.ready and july.ready
    assert january.forecasts.expected_return.min() > 0.01
    assert july.forecasts.expected_return.max() < -0.01
    assert january.audit["seasonal_ridge_penalty"] == 20
    assert january.audit["annual_period_days"] == 365.25


@pytest.mark.parametrize("seasonal", [False, True])
def test_excluded_products_cannot_change_fit_or_force_invalid_numeric_reads(seasonal):
    sample = samples()
    initial = predict(sample, seasonal=seasonal, excluded_products=("SA", "FG"))
    sample.loc[
        sample["product"].isin(["SA", "FG"]),
        ["gross_return", "volatility", "curve_level", "curve_change", "pressure"],
    ] = np.nan
    changed = predict(sample, seasonal=seasonal, excluded_products=("SA", "FG"))
    pd.testing.assert_frame_equal(changed.forecasts, initial.forecasts)
    assert changed.audit["training_products"] == 20


def test_same_day_and_future_labels_are_never_numerically_consumed():
    sample = samples()
    initial = predict(sample, seasonal=True, grouped=True, groups=GROUPS)
    future = sample.iloc[:22].copy()
    future["entry_day"] = pd.Timestamp("2024-12-26")
    future["maturity_day"] = pd.Timestamp("2025-01-02")
    future[["gross_return", "volatility", "curve_level"]] = np.nan
    changed = predict(pd.concat([sample, future]), seasonal=True, grouped=True, groups=GROUPS)
    pd.testing.assert_frame_equal(changed.forecasts, initial.forecasts)
    assert changed.audit["last_maturity_day"] < "2025-01-02"


def test_grouped_seasonal_fit_can_learn_opposite_industry_base_slopes():
    sample = samples()
    sample["gross_return"] = sample["product"].map(GROUPS).map({"one": 0.02, "two": -0.02})
    result = predict(sample, seasonal=True, grouped=True, groups=GROUPS)
    rows = result.forecasts.assign(group=result.forecasts["product"].map(GROUPS))
    assert rows.loc[rows.group.eq("one"), "expected_return"].min() > 0
    assert rows.loc[rows.group.eq("two"), "expected_return"].max() < 0
    assert result.audit["group_deviation_penalty"] == 20


def test_insufficient_breadth_returns_no_synthetic_predictions():
    result = predict(samples(), seasonal=True, excluded_products=("AG", "SA", "FG", "JM"))
    assert not result.ready and result.forecasts.empty
    assert result.audit["training_products"] == 19


def test_completed_feature_timing_and_group_mapping_fail_closed():
    sample = samples()
    sample["feature_through"] = sample.entry_day
    with pytest.raises(ValueError, match="features must precede"):
        predict(sample, seasonal=True)
    with pytest.raises(ValueError, match="missing predeclared"):
        predict(samples(), seasonal=True, grouped=True, groups={})


@pytest.mark.parametrize("exclusions", ["FG", ("fg",), (3,)])
def test_noncanonical_exclusion_inputs_fail_closed(exclusions):
    with pytest.raises(ValueError, match="exclusions must"):
        predict(samples(), seasonal=True, excluded_products=exclusions)
