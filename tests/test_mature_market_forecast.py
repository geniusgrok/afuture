from math import sqrt, tanh

import numpy as np
import pandas as pd
import pytest

from afuture.mature_market_forecast import (
    completed_pressure_features,
    forecast_from_mature_market,
)

PRODUCTS = tuple("A B C M Y P I J JM L V PP EG EB TA MA SA FG RB HC".split())


def observations(periods=126):
    rows = []
    for day in pd.bdate_range("2023-01-02", periods=periods):
        for position, product in enumerate(PRODUCTS):
            sign = 1.0 if position % 2 else -1.0
            rows.append(
                dict(
                    entry_day=day,
                    maturity_day=day + pd.offsets.BDay(5),
                    product=product,
                    symbol=product + "2401",
                    x1=sign,
                    x2=0.0,
                    x3=0.0,
                    volatility=0.01,
                    gross_return=sign * 0.01 * sqrt(5),
                )
            )
    return pd.DataFrame(rows)


def targets(day="2025-01-02"):
    return pd.DataFrame(
        [
            dict(entry_day=day, product="A", symbol="A2505", x1=-1, x2=0, x3=0, volatility=0.01),
            dict(entry_day=day, product="B", symbol="B2505", x1=1, x2=0, x3=0, volatility=0.02),
        ]
    )


def forecast(sample, target=None, **kwargs):
    return forecast_from_mature_market(
        sample, targets() if target is None else target, decision_day="2025-01-02", **kwargs
    )


def test_completed_pressure_features_use_real_same_bar_values():
    result = completed_pressure_features(
        open_price=100,
        high=110,
        low=90,
        close=110,
        previous_close=95,
        open_interest=1100,
        previous_open_interest=1000,
        volume=500,
    )
    assert result == pytest.approx({"x1": 1, "x2": 1 / 3, "x3": tanh(0.2)})


def test_valid_motionless_bar_has_zero_features():
    assert completed_pressure_features(
        open_price=100,
        high=100,
        low=100,
        close=100,
        previous_close=100,
        open_interest=1000,
        previous_open_interest=1100,
        volume=500,
    ) == dict(x1=0.0, x2=0.0, x3=0.0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("close", np.nan),
        ("high", 99),
        ("volume", 0),
        ("open_interest", -1),
        ("previous_close", None),
        ("volume", True),
    ],
)
def test_bad_completed_bar_is_not_neutral_evidence(field, value):
    bar = dict(
        open_price=100,
        high=105,
        low=95,
        close=101,
        previous_close=99,
        open_interest=1000,
        previous_open_interest=999,
        volume=500,
    )
    bar[field] = value
    with pytest.raises(ValueError):
        completed_pressure_features(**bar)


def test_signed_forecast_and_volatility_denormalization_match_closed_form():
    result = forecast(observations())
    assert result.ready
    assert result.audit["coefficients"] == pytest.approx(dict(x1=126 / 127, x2=0, x3=0))
    assert result.forecasts.expected_return.tolist() == pytest.approx(
        [-126 / 127 * 0.01 * sqrt(5), 126 / 127 * 0.02 * sqrt(5)]
    )
    assert result.audit["training_records"] == 2520
    assert result.audit["sample_weight_sum"] == pytest.approx(126)


def test_labels_maturing_today_or_later_cannot_affect_today():
    sample = observations()
    future = sample.iloc[:3].copy()
    future["entry_day"] = pd.Timestamp("2024-12-30")
    future["maturity_day"] = [pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-03"), pd.NaT]
    future["gross_return"] = [np.nan, 1e200, np.nan]
    future["volatility"] = np.nan
    baseline = forecast(sample)
    result = forecast(pd.concat([sample, future], ignore_index=True))
    pd.testing.assert_frame_equal(result.forecasts, baseline.forecasts)
    assert result.audit == baseline.audit


def test_market_observations_update_even_when_account_never_traded():
    sample = observations()
    sample["account_held"] = False  # Not an estimator input or selection condition.
    initial = forecast(sample)
    sample["gross_return"] *= -1
    changed = forecast(sample)
    assert changed.audit["training_records"] == initial.audit["training_records"]
    assert changed.forecasts.expected_return.to_numpy() == pytest.approx(
        -initial.forecasts.expected_return.to_numpy()
    )


def test_exag_removes_ag_before_normalizing_labels_and_fitting():
    sample = observations()
    ag = sample.loc[sample["product"] == "A"].copy()
    ag["product"], ag["symbol"] = "AG", "AG2401"
    ag["gross_return"], ag["volatility"] = 1e100, np.nan
    ag_target = targets().iloc[:1].copy()
    ag_target["product"], ag_target["symbol"], ag_target["volatility"] = "AG", "AG2505", np.nan
    result = forecast(pd.concat([sample, ag]), pd.concat([targets(), ag_target]), exclude_ag=True)
    expected = forecast(sample, exclude_ag=True)
    pd.testing.assert_frame_equal(result.forecasts, expected.forecasts)
    assert result.audit == expected.audit
    with pytest.raises(ValueError, match="non-finite"):
        forecast(pd.concat([sample, ag]))


def test_date_has_equal_weight_despite_different_numbers_of_products():
    sample = observations()
    sparse_day = sample.entry_day.min()
    sample = sample.loc[(sample.entry_day != sparse_day) | (sample["product"] == "A")].copy()
    sample.loc[sample.entry_day == sparse_day, "gross_return"] *= -1
    result = forecast(sample)
    # 125 normal days and one opposite day, each with total weight one.
    assert result.audit["coefficients"]["x1"] == pytest.approx(124 / 127)
    assert result.audit["entry_day_sample_counts"]["2023-01-02"] == 1


def test_only_latest_252_mature_entry_dates_are_used():
    sample = observations(periods=270)
    expected = forecast(sample.loc[sample.entry_day >= sorted(sample.entry_day.unique())[-252]])
    sample.loc[sample.entry_day < sorted(sample.entry_day.unique())[-252], "gross_return"] = np.nan
    result = forecast(sample)
    assert result.audit["training_entry_days"] == 252
    assert result.audit == expected.audit
    pd.testing.assert_frame_equal(result.forecasts, expected.forecasts)


@pytest.mark.parametrize("kind", ["few_dates", "few_products", "empty"])
def test_insufficient_sample_returns_no_forecasts(kind):
    sample = observations(periods=125 if kind == "few_dates" else 126)
    if kind == "few_products":
        sample = sample.loc[sample["product"] != "A"]
    elif kind == "empty":
        sample = sample.iloc[:0]
    result = forecast(sample)
    assert not result.ready
    assert result.forecasts.empty
    assert result.audit["coefficients"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("x1", np.nan),
        ("x2", 1.01),
        ("volatility", 0),
        ("gross_return", -1),
        ("maturity_day", "2023-01-02"),
        ("entry_day", "2023-01-02T12:00:00"),
        ("symbol", ""),
        ("symbol", "AG2401"),
    ],
)
def test_bad_training_input_fails_closed(field, value):
    sample = observations()
    sample.loc[0, field] = value
    with pytest.raises(ValueError):
        forecast(sample)


def test_duplicate_training_identity_and_missing_schema_fail_closed():
    sample = observations()
    with pytest.raises(ValueError, match="one selected contract"):
        forecast(pd.concat([sample, sample.iloc[:1]]))
    with pytest.raises(ValueError, match="schema"):
        forecast(sample.drop(columns="volatility"))


def test_prediction_evidence_must_be_valid_and_for_the_decision_day():
    with pytest.raises(ValueError, match="entry day"):
        forecast(observations(), targets("2025-01-03"))
    target = targets()
    target.loc[0, "volatility"] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        forecast(observations(), target)
