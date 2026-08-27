import pandas as pd
import pytest

# TDD contract: reversing into a new side requires completed D->D+1 OI confirmation.


def _api():
    from afuture.directional_60m_oi_reversal_confirmation import (
        apply_oi_confirmation_to_direction_changes,
    )

    return apply_oi_confirmation_to_direction_changes


def test_confirmed_new_entry_and_reversal_are_allowed():
    apply_filter = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    raw = pd.DataFrame({"A": [0.0, 1.0, -1.0]}, index=index)
    flow = pd.DataFrame({"A": [0.0, 1.0, -1.0]}, index=index)

    result = apply_filter(
        raw_weights=raw,
        confirming_flow=flow,
        supported_products=("A",),
    )

    assert list(result["A"]) == [0.0, 1.0, -1.0]


def test_unconfirmed_reversal_exits_old_side_instead_of_preserving_it():
    apply_filter = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame({"A": [1.0, -1.0]}, index=index)
    flow = pd.DataFrame({"A": [1.0, 1.0]}, index=index)

    result = apply_filter(
        raw_weights=raw,
        confirming_flow=flow,
        supported_products=("A",),
    )

    assert result.loc[index[0], "A"] == 1.0
    assert result.loc[index[1], "A"] == 0.0


def test_unconfirmed_same_sign_increase_holds_prior_but_reduction_and_exit_bypass():
    apply_filter = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"])
    raw = pd.DataFrame({"A": [1.0, 2.0, 0.5, 0.0]}, index=index)
    flow = pd.DataFrame({"A": [1.0, -1.0, -1.0, -1.0]}, index=index)

    result = apply_filter(
        raw_weights=raw,
        confirming_flow=flow,
        supported_products=("A",),
    )

    assert list(result["A"]) == [1.0, 1.0, 0.5, 0.0]


def test_unsupported_products_are_exactly_unchanged_and_gross_never_increases():
    apply_filter = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame({"A": [1.0, -1.0], "AG": [-1.0, 1.0]}, index=index)
    flow = pd.DataFrame({"A": [1.0, 1.0]}, index=index)

    result = apply_filter(
        raw_weights=raw,
        confirming_flow=flow,
        supported_products=("A",),
    )

    assert result["AG"].equals(raw["AG"])
    assert bool((result.abs().sum(axis=1) <= raw.abs().sum(axis=1) + 1e-12).all())


def test_supported_product_missing_flow_fails_instead_of_becoming_zero():
    apply_filter = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame({"A": [0.0, 1.0]}, index=index)
    flow = pd.DataFrame({"A": [0.0, float("nan")]}, index=index)

    with pytest.raises(ValueError, match="missing completed OI flow: A"):
        apply_filter(
            raw_weights=raw,
            confirming_flow=flow,
            supported_products=("A",),
        )
