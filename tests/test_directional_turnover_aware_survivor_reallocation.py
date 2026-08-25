import pandas as pd


def _api():
    from afuture.directional_turnover_aware_survivor_reallocation import (
        reallocate_survivors_lexicographically,
    )

    return reallocate_survivors_lexicographically


def test_reallocation_preserves_only_survivor_support_sign_and_original_gross():
    reallocate = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    original = pd.DataFrame({"A": [1.0, 1.0], "M": [-0.5, -0.5], "I": [0.5, 0.5]}, index=index)
    approved = pd.DataFrame({"A": [1.0, 1.0], "M": [-0.5, 0.0], "I": [0.0, 0.5]}, index=index)

    result = reallocate(original_weights=original, approved_weights=approved)

    assert (result.abs().sum(axis=1) - original.abs().sum(axis=1)).abs().max() < 1e-12
    assert result.loc[index[0], "I"] == 0.0
    assert result.loc[index[1], "M"] == 0.0
    assert result.loc[index[0], "A"] > 0.0
    assert result.loc[index[0], "M"] < 0.0
    assert result.loc[index[1], "A"] > 0.0
    assert result.loc[index[1], "I"] > 0.0


def test_lexicographic_tie_break_prefers_previous_survivor_allocation():
    reallocate = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    # The cost layer is an eligibility mask here: survivor magnitudes first track the
    # original OI target as closely as possible. Day 1 leaves only A eligible, so A gets
    # the full 2x budget. On day 2 A and M survive while C is rejected; after restoring
    # their original 0.5/0.5 target magnitudes, one extra unit is L1-tracking indifferent.
    # The secondary turnover objective keeps that residual in A, closest to yesterday.
    original = pd.DataFrame({"A": [1.0, 0.5], "M": [1.0, 0.5], "C": [0.0, 1.0]}, index=index)
    approved = pd.DataFrame({"A": [1.0, 0.5], "M": [0.0, 0.5], "C": [0.0, 0.0]}, index=index)

    result = reallocate(original_weights=original, approved_weights=approved)

    assert result.loc[index[0], "A"] == 2.0
    assert result.loc[index[0], "M"] == 0.0
    assert result.loc[index[1], "A"] == 1.5
    assert result.loc[index[1], "M"] == 0.5
    assert result.loc[index[1], "C"] == 0.0


def test_no_survivor_fails_closed_to_zero_instead_of_creating_support():
    reallocate = _api()
    index = pd.to_datetime(["2026-01-05"])
    original = pd.DataFrame({"A": [1.0], "M": [-1.0]}, index=index)
    approved = pd.DataFrame({"A": [0.0], "M": [0.0]}, index=index)

    result = reallocate(original_weights=original, approved_weights=approved)

    assert float(result.abs().sum().sum()) == 0.0


def test_reversal_does_not_preserve_previous_opposite_sign_allocation():
    reallocate = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    original = pd.DataFrame({"A": [2.0, -1.0], "M": [0.0, -1.0]}, index=index)
    approved = original.copy()

    result = reallocate(original_weights=original, approved_weights=approved)

    assert result.loc[index[1], "A"] < 0.0
    assert result.loc[index[1], "M"] < 0.0
    assert float(result.loc[index[1]].abs().sum()) == 2.0


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
