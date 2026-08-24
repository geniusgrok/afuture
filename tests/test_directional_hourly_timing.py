import pandas as pd


def test_quadrant_direction_is_classic_price_oi_state():
    from afuture.directional_hourly_timing import price_oi_direction

    assert price_oi_direction(price_return=0.01, oi_return=0.02) == 1
    assert price_oi_direction(price_return=-0.01, oi_return=0.02) == -1
    assert price_oi_direction(price_return=0.01, oi_return=-0.02) == -1
    assert price_oi_direction(price_return=-0.01, oi_return=-0.02) == 1
    assert price_oi_direction(price_return=0.0, oi_return=0.02) == 0


def test_disagreeing_hourly_state_suppresses_new_risk_but_allows_real_reduction():
    from afuture.directional_hourly_timing import apply_hourly_timing_overlay

    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    raw = pd.DataFrame({"M": [1.0, 2.0, 0.5]}, index=dates)
    state = pd.DataFrame({"M": [1.0, -1.0, -1.0]}, index=dates)

    result = apply_hourly_timing_overlay(raw, state)

    assert result.loc[dates[0], "M"] == 1.0
    assert result.loc[dates[1], "M"] == 1.0
    assert result.loc[dates[2], "M"] == 0.5


def test_reversal_bypasses_hourly_confirmation_even_when_state_disagrees():
    from afuture.directional_hourly_timing import apply_hourly_timing_overlay

    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    raw = pd.DataFrame({"M": [1.0, 2.0, -1.0]}, index=dates)
    state = pd.DataFrame({"M": [1.0, -1.0, 1.0]}, index=dates)

    result = apply_hourly_timing_overlay(raw, state)

    assert result.loc[dates[0], "M"] == 1.0
    assert result.loc[dates[1], "M"] == 1.0
    assert result.loc[dates[2], "M"] == -1.0


def test_exit_bypasses_hourly_confirmation():
    from afuture.directional_hourly_timing import apply_hourly_timing_overlay

    dates = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame({"M": [1.0, 0.0]}, index=dates)
    state = pd.DataFrame({"M": [1.0, -1.0]}, index=dates)

    result = apply_hourly_timing_overlay(raw, state)

    assert result.loc[dates[1], "M"] == 0.0


def test_missing_hourly_evidence_preserves_baseline_target():
    from afuture.directional_hourly_timing import apply_hourly_timing_overlay

    dates = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame({"M": [0.0, 1.0], "AG": [0.0, -1.0]}, index=dates)
    state = pd.DataFrame({"M": [0.0, float("nan")]}, index=dates)

    result = apply_hourly_timing_overlay(raw, state)

    assert result.loc[dates[1], "M"] == 1.0
    assert result.loc[dates[1], "AG"] == -1.0


def test_overlay_never_increases_raw_daily_gross():
    from afuture.directional_hourly_timing import apply_hourly_timing_overlay

    dates = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame({"M": [1.0, 1.0], "C": [-1.0, -1.0]}, index=dates)
    state = pd.DataFrame({"M": [1.0, -1.0], "C": [-1.0, 1.0]}, index=dates)

    result = apply_hourly_timing_overlay(raw, state)

    assert (result.abs().sum(axis=1) <= raw.abs().sum(axis=1) + 1e-12).all()
    assert (result.abs().sum(axis=1) <= 2.0 + 1e-12).all()
