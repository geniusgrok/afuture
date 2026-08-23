import math

import pytest

from afuture.directional import fit_target_lots_to_margin_budget


def _margin(lots, per_lot):
    return sum(abs(int(volume)) * float(per_lot[symbol]) for symbol, volume in lots.items())


def test_margin_budget_fits_stress_target_without_relaxing_hard_cap():
    # 20 lots * 1,875 margin/lot = 37,500, but the hard account budget is 35,000.
    requested = {"A2609": 20}
    per_lot = {"A2609": 1000.0 * 10.0 * 0.15 * 1.25}

    fitted = fit_target_lots_to_margin_budget(
        requested,
        per_lot,
        margin_budget=35000.0,
    )

    assert fitted == {"A2609": 18}
    assert _margin(fitted, per_lot) <= 35000.0
    assert abs(fitted["A2609"]) <= abs(requested["A2609"])


def test_margin_budget_preserves_sign_and_allocates_integer_residual_deterministically():
    requested = {"A2609": 5, "M2609": -5}
    per_lot = {"A2609": 100.0, "M2609": 200.0}

    fitted = fit_target_lots_to_margin_budget(
        requested,
        per_lot,
        margin_budget=1000.0,
    )

    assert fitted == {"A2609": 4, "M2609": -3}
    assert fitted["A2609"] > 0
    assert fitted["M2609"] < 0
    assert _margin(fitted, per_lot) <= 1000.0
    for symbol in requested:
        assert abs(fitted.get(symbol, 0)) <= abs(requested[symbol])


def test_margin_budget_fails_closed_when_positive_target_has_no_margin_estimate():
    with pytest.raises(ValueError, match="missing positive per-lot margin: A2609"):
        fit_target_lots_to_margin_budget(
            {"A2609": 1},
            {},
            margin_budget=35000.0,
        )


def test_margin_budget_rejects_invalid_budget():
    with pytest.raises(ValueError, match="margin_budget cannot be negative"):
        fit_target_lots_to_margin_budget(
            {"A2609": 1},
            {"A2609": 100.0},
            margin_budget=-1.0,
        )
