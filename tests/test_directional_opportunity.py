from __future__ import annotations

import numpy as np
import pandas as pd

from afuture.directional_opportunity import (
    OPPORTUNITY_CORE_SHARE,
    apply_opportunity_overlay,
    build_opportunity_score,
)


def _frame(values, columns=("A", "B", "C", "D")) -> pd.DataFrame:
    index = pd.bdate_range("2026-01-01", periods=len(values))
    return pd.DataFrame(values, index=index, columns=columns, dtype=float)


def test_opportunity_overlay_keeps_top_half_and_only_deemphasizes_lower_ranked_risk():
    index = pd.DatetimeIndex([pd.Timestamp("2026-08-20")])
    raw = pd.DataFrame(
        [[0.8, -0.6, 0.4, -0.2]], index=index, columns=["A", "B", "C", "D"]
    )
    score = pd.DataFrame(
        [[0.9, 0.2, 0.8, 0.1]], index=index, columns=raw.columns
    )

    actual = apply_opportunity_overlay(raw, score)

    assert OPPORTUNITY_CORE_SHARE == 0.75
    assert actual.loc[index[0], "A"] == raw.loc[index[0], "A"]
    assert actual.loc[index[0], "C"] == raw.loc[index[0], "C"]
    assert actual.loc[index[0], "B"] == raw.loc[index[0], "B"] * 0.75
    assert actual.loc[index[0], "D"] == raw.loc[index[0], "D"] * 0.75
    assert bool((actual.abs() <= raw.abs() + 1e-12).all().all())
    assert bool(((actual * raw) >= -1e-12).all().all())
    assert bool(
        actual.abs().sum(axis=1).le(raw.abs().sum(axis=1) + 1e-12).all()
    )


def test_opportunity_overlay_cannot_create_exposure_and_fails_open_without_evidence():
    index = pd.DatetimeIndex([pd.Timestamp("2026-08-20")])
    raw = pd.DataFrame([[1.0, 0.0, -1.0]], index=index, columns=["A", "B", "C"])
    missing = pd.DataFrame([[np.nan, np.nan, np.nan]], index=index, columns=raw.columns)

    actual = apply_opportunity_overlay(raw, missing)

    pd.testing.assert_frame_equal(actual, raw)
    assert actual.loc[index[0], "B"] == 0.0


def test_opportunity_score_uses_only_completed_prior_rows():
    index = pd.bdate_range("2026-01-01", periods=90)
    base = np.linspace(100.0, 130.0, len(index))
    close = pd.DataFrame(
        {
            "A": base,
            "B": 120.0 + np.sin(np.arange(len(index))) * 2.0,
            "C": np.linspace(90.0, 105.0, len(index)),
        },
        index=index,
    )
    volume = pd.DataFrame(
        {
            "A": np.linspace(1000.0, 3000.0, len(index)),
            "B": np.linspace(3000.0, 1000.0, len(index)),
            "C": np.linspace(1500.0, 2200.0, len(index)),
        },
        index=index,
    )
    open_interest = volume * 10.0

    before = build_opportunity_score(close, volume, open_interest)
    changed_close = close.copy()
    changed_volume = volume.copy()
    changed_oi = open_interest.copy()
    changed_close.iloc[-1, :] *= [1.25, 0.75, 1.15]
    changed_volume.iloc[-1, :] *= [10.0, 0.1, 5.0]
    changed_oi.iloc[-1, :] *= [0.1, 10.0, 3.0]
    after = build_opportunity_score(changed_close, changed_volume, changed_oi)

    pd.testing.assert_series_equal(before.iloc[-1], after.iloc[-1])
    assert before.iloc[:60].isna().all(axis=None)
    assert before.iloc[-1].notna().any()


def test_opportunity_score_requires_activity_evidence_instead_of_inventing_it():
    index = pd.bdate_range("2026-01-01", periods=90)
    close = pd.DataFrame(
        {
            "A": np.linspace(100.0, 130.0, len(index)),
            "B": np.linspace(100.0, 115.0, len(index)),
        },
        index=index,
    )

    actual = build_opportunity_score(close, None, None)

    assert actual.isna().all(axis=None)
