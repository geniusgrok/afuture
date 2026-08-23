from statistics import stdev

import pytest

from afuture.directional_capacity_research import shock_derived_margin_sizing_share


def test_shock_derived_share_uses_hard_limits_completed_returns_and_stress_rotation_cost():
    common = dict(
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        max_gross_ratio=2.0,
        stress_cost_bps=15.0,
    )
    calm = shock_derived_margin_sizing_share(completed_returns=(), **common)
    # hard share * (1 - configured 5% shock - 4x-notional * 15bp full-reversal reserve)
    assert calm == pytest.approx(0.35 * (1.0 - 0.05 - 0.006))

    stressed = shock_derived_margin_sizing_share(
        completed_returns=(-0.10, 0.01), **common
    )
    assert stressed < calm
    assert stressed == pytest.approx(0.35 * (1.0 - max(0.05, 0.10, stdev((-0.10, 0.01))) - 0.006))
    assert 0.0 <= stressed <= 0.35


def test_shock_derived_share_never_uses_future_or_expands_hard_margin_gate():
    common = dict(
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        max_gross_ratio=2.0,
        stress_cost_bps=15.0,
    )
    before = shock_derived_margin_sizing_share(completed_returns=(0.01, -0.02), **common)
    # A caller can only pass already completed observations. Appending a later observation
    # changes a later decision, not the earlier value just computed.
    after = shock_derived_margin_sizing_share(completed_returns=(0.01, -0.02, -0.20), **common)
    assert before <= common["max_margin_ratio"]
    assert after <= before
    assert after >= 0.0
