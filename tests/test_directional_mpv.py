from datetime import date
import math

import pytest

from afuture.directional_mpv import MarginalLotAction, score_marginal_production_value


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
