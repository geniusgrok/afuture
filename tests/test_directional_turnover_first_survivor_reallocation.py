import pandas as pd


def _api():
    from afuture.directional_turnover_first_survivor_reallocation import (
        reallocate_survivors_turnover_first,
    )

    return reallocate_survivors_turnover_first


def test_turnover_first_keeps_previous_survivor_allocation_when_feasible():
    reallocate = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    original = pd.DataFrame(
        {"A": [1.0, 0.5], "M": [1.0, 0.5], "C": [0.0, 1.0]}, index=index
    )
    approved = pd.DataFrame(
        {"A": [1.0, 0.5], "M": [0.0, 0.5], "C": [0.0, 0.0]}, index=index
    )

    result = reallocate(original_weights=original, approved_weights=approved)

    # Day 1: only A survives, so the 2x budget sits in A.
    assert result.loc[index[0], "A"] == 2.0
    assert result.loc[index[0], "M"] == 0.0
    # Day 2: A remains an approved same-sign survivor and can carry the full gross.
    # Primary minimum-turnover therefore keeps A at 2x rather than rotating into M.
    assert result.loc[index[1], "A"] == 2.0
    assert result.loc[index[1], "M"] == 0.0
    assert result.loc[index[1], "C"] == 0.0


def test_when_previous_gross_is_insufficient_secondary_tracks_then_tertiary_is_proportional():
    reallocate = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    original = pd.DataFrame(
        {"A": [1.0, 0.5], "M": [0.0, 0.5], "C": [0.0, 1.0]}, index=index
    )
    approved = pd.DataFrame(
        {"A": [1.0, 0.5], "M": [0.0, 0.5], "C": [0.0, 0.0]}, index=index
    )

    result = reallocate(original_weights=original, approved_weights=approved)

    assert result.loc[index[0], "A"] == 1.0
    # Day 2 needs one extra gross unit beyond yesterday's A=1. First fill M's 0.5
    # original-target deficit; the remaining 0.5 is tracking-indifferent and therefore
    # split in the original survivor 0.5/0.5 proportions.
    assert result.loc[index[1], "A"] == 1.25
    assert result.loc[index[1], "M"] == 0.75


def test_support_sign_and_original_gross_are_hard_constraints():
    reallocate = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    original = pd.DataFrame(
        {"A": [1.0, -1.0], "M": [-0.5, -0.5], "I": [0.5, 0.5]}, index=index
    )
    approved = pd.DataFrame(
        {"A": [1.0, -1.0], "M": [-0.5, 0.0], "I": [0.0, 0.5]}, index=index
    )

    result = reallocate(original_weights=original, approved_weights=approved)

    assert (result.abs().sum(axis=1) - original.abs().sum(axis=1)).abs().max() < 1e-12
    assert result.loc[index[0], "I"] == 0.0
    assert result.loc[index[1], "M"] == 0.0
    assert result.loc[index[1], "A"] < 0.0
    assert result.loc[index[1], "I"] > 0.0


def test_no_survivor_fails_closed_to_zero():
    reallocate = _api()
    index = pd.to_datetime(["2026-01-05"])
    original = pd.DataFrame({"A": [1.0], "M": [-1.0]}, index=index)
    approved = pd.DataFrame({"A": [0.0], "M": [0.0]}, index=index)

    result = reallocate(original_weights=original, approved_weights=approved)
    assert float(result.abs().sum().sum()) == 0.0


def test_invalid_new_support_is_rejected():
    reallocate = _api()
    index = pd.to_datetime(["2026-01-05"])
    original = pd.DataFrame({"A": [1.0], "M": [0.0]}, index=index)
    approved = pd.DataFrame({"A": [1.0], "M": [0.5]}, index=index)

    try:
        reallocate(original_weights=original, approved_weights=approved)
    except ValueError as exc:
        assert "new product support" in str(exc)
    else:
        raise AssertionError("expected new-support rejection")
