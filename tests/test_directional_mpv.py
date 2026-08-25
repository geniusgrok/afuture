import inspect
import math
from datetime import date

import pandas as pd
import pytest

from afuture.directional_mpv import (
    MarginalLotAction,
    estimate_causal_product_value,
    score_marginal_production_value,
)


def valid_action(**overrides):
    values = dict(
        decision_date=date(2026, 8, 20),
        evidence_through=date(2026, 8, 19),
        product="AG",
        symbol="AG2612",
        delta_lots=1,
        current_lots=2,
        requested_lots=4,
        lot_notional=120000.0,
        per_lot_margin=18000.0,
        equity=500000.0,
        margin_utilization=0.18,
        available_ratio=0.82,
        governor_scale=1.0,
        roll_required=False,
    )
    values.update(overrides)
    return MarginalLotAction(**values)


def test_mpv_total_is_expected_gross_alpha_less_all_production_drags():
    action = valid_action()
    value = score_marginal_production_value(
        action,
        expected_incremental_gross_alpha=900.0,
        expected_incremental_transaction_cost=180.0,
        margin_opportunity_cost=40.0,
        turnover_penalty=30.0,
        downside_risk_penalty=100.0,
        correlation_penalty=50.0,
        concentration_penalty=25.0,
        roll_execution_penalty=0.0,
    )
    assert value.total_value == 475.0


def test_mpv_rejects_future_dated_evidence():
    with pytest.raises(ValueError, match="evidence_through"):
        valid_action(evidence_through=date(2026, 8, 20))


def test_mpv_rejects_action_that_opens_against_raw_requested_direction():
    with pytest.raises(ValueError, match="requested"):
        valid_action(
            product="CU",
            symbol="CU2610",
            current_lots=0,
            requested_lots=-2,
            delta_lots=1,
        )


def test_mpv_allows_reduction_against_requested_direction():
    action = valid_action(
        product="CU",
        symbol="CU2610",
        current_lots=-2,
        requested_lots=-2,
        delta_lots=1,
    )
    assert action.delta_lots == 1


def test_mpv_rejects_non_finite_or_negative_drag():
    action = valid_action()
    kwargs = dict(
        expected_incremental_gross_alpha=900.0,
        expected_incremental_transaction_cost=180.0,
        margin_opportunity_cost=40.0,
        turnover_penalty=30.0,
        downside_risk_penalty=100.0,
        correlation_penalty=50.0,
        concentration_penalty=25.0,
        roll_execution_penalty=0.0,
    )
    with pytest.raises(ValueError, match="finite"):
        score_marginal_production_value(
            action,
            **(kwargs | {"expected_incremental_gross_alpha": math.nan}),
        )
    with pytest.raises(ValueError, match="nonnegative"):
        score_marginal_production_value(
            action,
            **(kwargs | {"turnover_penalty": -1.0}),
        )


def test_causal_product_value_uses_data_derived_shrinkage_and_global_fallback():
    evidence = pd.DataFrame(
        {
            "gross_pnl": [300.0, 1000.0],
            "transaction_cost": [15.0, 150.0],
            "net_alpha": [285.0, 850.0],
            "lot_segment_exposure": [1.0, 10.0],
        },
        index=["AG", "CU"],
    )
    estimate = estimate_causal_product_value(evidence=evidence, product="AG")
    assert estimate.product_rate_gross == 300.0
    assert estimate.global_rate_gross == 1300.0 / 11.0
    assert 0.0 < estimate.product_weight < 1.0
    assert estimate.prior_support == 5.5
    assert estimate.global_fallback_weight == 1.0 - estimate.product_weight
    assert set(inspect.signature(estimate_causal_product_value).parameters) == {
        "evidence",
        "product",
    }

    missing = estimate_causal_product_value(evidence=evidence, product="ZN")
    assert missing.product_weight == 0.0
    assert missing.expected_gross_alpha_per_lot_segment == missing.global_rate_gross


def test_causal_product_value_moves_toward_product_rate_as_completed_support_grows():
    small = pd.DataFrame(
        {
            "gross_pnl": [300.0, 1000.0],
            "transaction_cost": [0.0, 0.0],
            "net_alpha": [300.0, 1000.0],
            "lot_segment_exposure": [1.0, 10.0],
        },
        index=["AG", "CU"],
    )
    large = pd.DataFrame(
        {
            "gross_pnl": [6000.0, 1000.0],
            "transaction_cost": [0.0, 0.0],
            "net_alpha": [6000.0, 1000.0],
            "lot_segment_exposure": [20.0, 10.0],
        },
        index=["AG", "CU"],
    )
    small_est = estimate_causal_product_value(evidence=small, product="AG")
    large_est = estimate_causal_product_value(evidence=large, product="AG")
    assert abs(large_est.expected_gross_alpha_per_lot_segment - 300.0) < abs(
        small_est.expected_gross_alpha_per_lot_segment - 300.0
    )


def test_causal_product_value_fails_closed_without_exposure_support():
    evidence = pd.DataFrame(
        {
            "gross_pnl": [100.0],
            "transaction_cost": [10.0],
            "net_alpha": [90.0],
            "lot_segment_exposure": [0.0],
        },
        index=["AG"],
    )
    estimate = estimate_causal_product_value(evidence=evidence, product="AG")
    assert estimate.insufficient_evidence is True
