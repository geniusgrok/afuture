from math import sqrt

import numpy as np
import pandas as pd
import pytest

from afuture.curve_experts import (
    ROUND_TRIP_SCORE_COST,
    choose_mature_expert,
    forecast_curve_expert,
)
from afuture.curve_rebuild import ANNUAL_PERIOD_DAYS

PRODUCTS = tuple("A B C M Y P I J JM L V PP EG EB TA MA SA FG RB HC CU AL AG".split())
GROUPS = {product: "first" if i < 11 else "second" for i, product in enumerate(PRODUCTS)}


def samples():
    days = pd.bdate_range("2022-01-03", periods=300)
    frame = pd.DataFrame(
        [(day, product) for day in days for product in PRODUCTS], columns=["entry_day", "product"]
    )
    frame["symbol"] = frame["product"] + "2501"
    frame["maturity_day"] = frame.entry_day + pd.offsets.BDay(5)
    frame["feature_through"] = frame.entry_day - pd.offsets.BDay(1)
    frame["volatility"] = 0.01
    frame["curve_level"] = 1.0
    frame["curve_change"] = 0.0
    frame["pressure"] = 0.0
    elapsed = (frame.entry_day - pd.Timestamp("2000-01-01")).dt.days
    frame["parent_expected_return"] = 0.005
    frame["gross_return"] = frame.parent_expected_return + np.cos(
        2 * np.pi * elapsed / ANNUAL_PERIOD_DAYS
    ) * 0.01 * sqrt(5)
    frame["parent_training_through"] = frame.feature_through
    frame["parent_decision_day"] = frame.entry_day
    frame["parent_product"] = frame["product"]
    frame["parent_symbol"] = frame.symbol
    return frame


def targets(sample, day="2025-01-02"):
    target = sample.iloc[: len(PRODUCTS)].drop(columns=["maturity_day", "gross_return"]).copy()
    target["entry_day"] = pd.Timestamp(day)
    target["feature_through"] = target.entry_day - pd.offsets.BDay(1)
    target["parent_training_through"] = target.feature_through
    target["parent_decision_day"] = target.entry_day
    return target


def predict(sample, mechanism="seasonal_residual", *, day="2025-01-02", **kwargs):
    return forecast_curve_expert(
        sample,
        targets(sample, day),
        decision_day=day,
        mechanism=mechanism,
        groups=GROUPS,
        **kwargs,
    )


def archive(sample, *, mu, day="2025-01-02"):
    all_rows = pd.concat([sample, targets(sample, day)], ignore_index=True)
    result = all_rows[["entry_day", "product", "symbol", "volatility"]].copy()
    result["decision_day"] = result.entry_day
    result["training_through"] = result.entry_day - pd.offsets.BDay(1)
    result["expected_return"] = mu
    return result


def selector(sample, experts, *, day="2025-01-02", **kwargs):
    return choose_mature_expert(experts, sample, decision_day=day, groups=GROUPS, **kwargs)


def test_additive_seasonal_residual_preserves_parent_and_learns_increment():
    sample = samples()
    added = predict(sample)
    only = predict(sample, "residual_only")
    assert added.ready and only.ready
    np.testing.assert_allclose(
        added.forecasts.expected_return - 0.005, only.forecasts.expected_return
    )
    assert only.forecasts.expected_return.min() > 0.01
    assert set(added.forecasts["product"]) == set(PRODUCTS)
    assert {"SA", "FG"}.issubset(added.forecasts["product"])
    assert added.audit["training_entry_days"] == 252
    assert added.audit["parent_predictions"] == "archived_chronological_out_of_fold"
    assert added.audit["last_maturity_day"] == "2025-01-01"
    assert added.audit["residual_last_maturity_day"] < "2025-01-01"


def test_perfect_parent_errors_produce_no_artificial_correction():
    sample = samples()
    sample["gross_return"] = sample.parent_expected_return
    assert predict(sample, "residual_only").forecasts.expected_return.eq(0).all()
    assert predict(sample).forecasts.expected_return.eq(0.005).all()


@pytest.mark.parametrize("field", ["parent_training_through", "parent_decision_day"])
def test_in_sample_or_wrong_date_parent_prediction_is_rejected(field):
    sample = samples()
    sample.loc[0, field] = (
        sample.loc[0, "entry_day"]
        if field == "parent_training_through"
        else sample.loc[0, "entry_day"] - pd.offsets.BDay(1)
    )
    with pytest.raises(ValueError, match="parent"):
        predict(sample)


@pytest.mark.parametrize("field", ["parent_product", "parent_symbol"])
def test_parent_contract_identity_must_match_the_actual_label(field):
    sample = samples()
    sample.loc[0, field] = "OTHER2501"
    with pytest.raises(ValueError, match="identity"):
        predict(sample)


def test_sparse_oof_dates_do_not_use_in_sample_filling_or_lower_readiness_gate():
    sample = samples()
    keep = sample.entry_day.unique()[:125]
    sample.loc[~sample.entry_day.isin(keep), "parent_expected_return"] = np.nan
    result = predict(sample)
    assert not result.ready and result.forecasts.empty
    assert result.audit["training_entry_days"] == 125
    assert result.audit["omitted_missing_parent_records"] == 175 * len(PRODUCTS)


@pytest.mark.parametrize(
    "mechanism", ["seasonal_residual", "residual_only", "bounded_nonlinear", "execution_net_core"]
)
def test_exag_exclusion_precedes_training_and_invalid_evidence(mechanism):
    sample = samples()
    before = predict(sample, mechanism, excluded_products=("AG",))
    sample.loc[sample["product"].eq("AG"), "gross_return"] = np.nan
    sample.loc[sample["product"].eq("AG"), "volatility"] = np.nan
    sample.loc[sample["product"].eq("AG"), "parent_training_through"] = pd.NaT
    after = predict(sample, mechanism, excluded_products=("AG",))
    pd.testing.assert_frame_equal(before.forecasts, after.forecasts)
    assert after.audit["training_products"] == 22
    assert not after.forecasts["product"].eq("AG").any()


@pytest.mark.parametrize(
    "mechanism", ["seasonal_residual", "residual_only", "bounded_nonlinear", "execution_net_core"]
)
def test_same_day_and_future_labels_do_not_change_any_expert(mechanism):
    sample = samples()
    initial = predict(sample, mechanism)
    future = sample.iloc[: len(PRODUCTS)].copy()
    future["entry_day"] = pd.Timestamp("2024-12-26")
    future["maturity_day"] = pd.Timestamp("2025-01-02")
    future[["gross_return", "volatility", "curve_level", "parent_expected_return"]] = np.nan
    future["parent_training_through"] = pd.NaT
    altered = predict(pd.concat([sample, future]), mechanism)
    pd.testing.assert_frame_equal(initial.forecasts, altered.forecasts)


def test_replacement_nonlinear_core_models_response_change_without_parent_errors():
    sample = samples()
    sample["curve_level"] = np.tile(np.linspace(-1, 1, len(PRODUCTS)), 300)
    sample["gross_return"] = (
        np.sign(sample.curve_level) * np.maximum(np.abs(sample.curve_level) - 0.5, 0) * 0.04
    )
    sample = sample.drop(columns=[c for c in sample if c.startswith("parent_")])
    result = predict(sample, "bounded_nonlinear")
    assert result.ready
    rows = result.forecasts.merge(targets(sample)[["product", "curve_level"]], on="product")
    assert rows.loc[rows.curve_level.eq(-1), "expected_return"].lt(0).all()
    assert rows.loc[rows.curve_level.eq(1), "expected_return"].gt(0).all()
    assert result.audit["normalized_target_limit"] == 3


@pytest.mark.parametrize("gross,sign", [(0.0, 0), (0.03, 1), (-0.03, -1)])
def test_execution_net_core_exposes_net_predictions_and_rejects_negative_edges(gross, sign):
    sample = samples()
    sample["gross_return"] = gross
    result = predict(sample, "execution_net_core")
    assert result.ready
    if sign == 0:
        assert result.forecasts.expected_return.eq(0).all()
        assert result.forecasts.expected_net_long.lt(0).all()
        assert result.forecasts.expected_net_short.lt(0).all()
    else:
        assert (np.sign(result.forecasts.expected_return) == sign).all()
        net = result.forecasts[["expected_net_long", "expected_net_short"]].max(axis=1)
        np.testing.assert_allclose(
            np.abs(result.forecasts.expected_return) - ROUND_TRIP_SCORE_COST, net
        )
    assert result.audit["expected_return_semantics"] == "signed_net_edge_plus_score_hurdle"


def test_causal_selector_chooses_different_experts_for_predeclared_groups():
    sample = samples()
    sample["gross_return"] = sample["product"].map(GROUPS).map({"first": 0.02, "second": -0.02})
    experts = {
        "C_T2": archive(sample, mu=0.0),
        "LONG": archive(sample, mu=0.01),
        "SHORT": archive(sample, mu=-0.01),
    }
    result = selector(sample, experts)
    assert result.ready
    assert result.audit["groups"]["first"]["chosen"] == "LONG"
    assert result.audit["groups"]["second"]["chosen"] == "SHORT"
    rows = result.forecasts.assign(group=result.forecasts["product"].map(GROUPS))
    assert rows.loc[rows.group.eq("first"), "expected_return"].eq(0.01).all()
    assert rows.loc[rows.group.eq("second"), "expected_return"].eq(-0.01).all()
    assert result.audit["last_maturity_day"] == "2025-01-01"
    assert result.audit["selection_last_maturity_day"] < "2025-01-01"


def test_selector_zero_or_negative_proxy_utilities_fall_back_to_parent():
    sample = samples()
    sample["gross_return"] = 0.0
    experts = {"C_T2": archive(sample, mu=0.001), "TRADE": archive(sample, mu=0.01)}
    result = selector(sample, experts)
    assert result.ready
    assert result.forecasts.expected_return.eq(0.001).all()
    assert all(audit["fallback"] for audit in result.audit["groups"].values())
    assert all(audit["utilities"]["TRADE"] < 0 for audit in result.audit["groups"].values())


def test_selector_never_scores_unmatured_labels_or_future_forecasts():
    sample = samples()
    sample["gross_return"] = 0.02
    experts = {"C_T2": archive(sample, mu=0), "LONG": archive(sample, mu=0.01)}
    initial = selector(sample, experts)
    pending = sample.iloc[: len(PRODUCTS)].copy()
    pending["entry_day"] = pd.Timestamp("2024-12-26")
    pending["maturity_day"] = pd.Timestamp("2025-01-02")
    pending["gross_return"] = np.nan
    later = archive(sample, mu=np.nan).iloc[-len(PRODUCTS) :].copy()
    later["entry_day"] = pd.Timestamp("2025-01-03")
    later[["decision_day", "training_through"]] = pd.NaT
    changed = {name: pd.concat([frame, later]) for name, frame in experts.items()}
    result = selector(pd.concat([sample, pending]), changed)
    pd.testing.assert_frame_equal(initial.forecasts, result.forecasts)
    assert initial.audit["groups"] == result.audit["groups"]


def test_selector_sparse_common_dates_do_not_relax_readiness():
    sample = samples()
    parent = archive(sample, mu=0.001)
    alternative = archive(sample, mu=0.01)
    keep = sample.entry_day.unique()[:125]
    alternative = alternative.loc[
        alternative.entry_day.isin(keep) | alternative.entry_day.eq(pd.Timestamp("2025-01-02"))
    ]
    result = selector(sample, {"C_T2": parent, "OTHER": alternative})
    assert not result.ready and result.forecasts.empty
    assert result.audit["training_entry_days"] == 125


def test_selector_group_gate_and_name_ties_remain_fixed():
    sample = samples()
    sample["gross_return"] = 0.02
    experts = {
        "Z_SAME": archive(sample, mu=0.01),
        "C_T2": archive(sample, mu=0.0),
        "A_SAME": archive(sample, mu=0.01),
    }
    keep = sample.entry_day.unique()[-59:]
    sample = sample.loc[~sample["product"].eq("AG") | sample.entry_day.isin(keep)]
    grouped = {**GROUPS, "AG": "new_group"}
    result = choose_mature_expert(experts, sample, decision_day="2025-01-02", groups=grouped)
    assert result.ready
    assert result.audit["groups"]["first"]["chosen"] == "A_SAME"
    assert result.audit["groups"]["new_group"]["training_entry_days"] == 59
    assert result.audit["groups"]["new_group"]["chosen"] == "C_T2"


@pytest.mark.parametrize("defect", ["duplicate", "future_training", "wrong_symbol"])
def test_selector_rejects_untrustworthy_oof_archives(defect):
    sample = samples()
    parent = archive(sample, mu=0.001)
    alternative = archive(sample, mu=0.01)
    if defect == "duplicate":
        alternative = pd.concat([alternative, alternative.iloc[:1]])
    elif defect == "future_training":
        alternative.loc[0, "training_through"] = alternative.loc[0, "entry_day"]
    else:
        alternative.loc[0, "symbol"] = "A2601"
    with pytest.raises(ValueError, match="duplicate|strictly precede|contract"):
        selector(sample, {"C_T2": parent, "OTHER": alternative})


def synthetic_missing_endpoint_inputs():
    sample = samples()
    sample["label_status"] = "observed"
    experts = {
        "C_T2": archive(sample, mu=0.0),
        "R_ADD": archive(sample, mu=0.01),
        "C_SEASON": archive(sample, mu=-0.01),
    }
    missing = sample.iloc[:4].copy()
    missing["entry_day"] = pd.to_datetime(["2024-12-12", "2024-12-19", "2024-12-19", "2024-12-19"])
    missing["feature_through"] = missing.entry_day - pd.offsets.BDay(1)
    missing["maturity_day"] = missing.entry_day + pd.offsets.BDay(5)
    missing["label_status"] = "unavailable_endpoint"
    missing["gross_return"] = np.nan
    for name, frame in experts.items():
        extra = missing[["entry_day", "product", "symbol", "volatility"]].copy()
        extra["decision_day"] = extra.entry_day
        extra["training_through"] = missing.feature_through
        extra["expected_return"] = frame.expected_return.iloc[0]
        experts[name] = pd.concat([frame, extra], ignore_index=True)
    return sample, experts, missing


def test_known_maturity_missing_endpoints_do_not_enter_selector_training():
    sample, experts, missing = synthetic_missing_endpoint_inputs()
    baseline = selector(sample, experts)
    changed = pd.concat([sample, missing], ignore_index=True)
    result = selector(changed, experts)
    pd.testing.assert_frame_equal(baseline.forecasts, result.forecasts)
    assert result.audit["omitted_mature_unobserved_labels"] == 4
    assert result.audit["training_entry_days"] == baseline.audit["training_entry_days"]
    assert result.audit["training_products"] == baseline.audit["training_products"]


@pytest.mark.parametrize("row_index", range(4))
def test_missing_endpoint_falsely_declared_observed_still_fails_closed(row_index):
    sample, experts, missing = synthetic_missing_endpoint_inputs()
    invalid = missing.iloc[[row_index]].assign(label_status="observed")
    changed = pd.concat([sample, invalid], ignore_index=True)
    with pytest.raises(ValueError, match="missing or non-finite"):
        selector(changed, experts)


def test_observed_label_with_invalid_completed_features_still_fails_closed():
    sample = samples()
    sample["label_status"] = "observed"
    experts = {"C_T2": archive(sample, mu=0), "OTHER": archive(sample, mu=0.01)}
    sample.loc[len(sample) - 1, "curve_level"] = np.nan
    with pytest.raises(ValueError, match="missing or non-finite"):
        selector(sample, experts)


def test_unobserved_rows_do_not_supply_selector_readiness_dates():
    sample = samples()
    sample["label_status"] = "observed"
    keep = sample.entry_day.unique()[:125]
    sample.loc[~sample.entry_day.isin(keep), "label_status"] = "unavailable_endpoint"
    experts = {"C_T2": archive(sample, mu=0), "OTHER": archive(sample, mu=0.01)}
    result = selector(sample, experts)
    assert not result.ready and result.forecasts.empty
    assert result.audit["training_entry_days"] == 125
    assert result.audit["omitted_mature_unobserved_labels"] == 175 * len(PRODUCTS)
