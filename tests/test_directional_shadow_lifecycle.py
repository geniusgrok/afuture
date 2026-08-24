import pandas as pd


def test_completed_signal_runs_exclude_still_open_final_run():
    from afuture.directional_shadow_lifecycle import build_completed_signal_runs

    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"])
    weights = pd.DataFrame({"AG": [1.0, 0.5, 0.0, -1.0, -1.0]}, index=dates)

    runs = build_completed_signal_runs(weights)

    assert len(runs) == 1
    row = runs.iloc[0]
    assert row["product"] == "AG"
    assert row["direction"] == 1
    assert row["sessions"] == 2
    assert row["start_date"] == pd.Timestamp("2026-01-05")
    assert row["end_date"] == pd.Timestamp("2026-01-06")
    assert row["label_available_date"] == pd.Timestamp("2026-01-07")


def test_run_completed_today_is_not_used_until_next_decision_day():
    from afuture.directional_shadow_lifecycle import build_causal_remaining_horizon_panel

    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"])
    weights = pd.DataFrame({"AG": [1.0, 1.0, 0.0, 1.0, 1.0]}, index=dates)

    panel = build_causal_remaining_horizon_panel(weights)

    # First run is not known complete until the zero target on Jan-07; strict causality
    # means it cannot affect Jan-07 itself. On Jan-08 the completed 2-session run is
    # available and the new run starts with a 2-session expected lifecycle.
    assert panel.loc[pd.Timestamp("2026-01-07"), "AG"] == 1.0
    assert panel.loc[pd.Timestamp("2026-01-08"), "AG"] == 2.0


def test_current_run_age_reduces_expected_remaining_lifecycle_without_going_below_one():
    from afuture.directional_shadow_lifecycle import build_causal_remaining_horizon_panel

    dates = pd.to_datetime(
        ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12"]
    )
    # Completed + run length=2, then a second + run starts and remains active for 3 days.
    weights = pd.DataFrame({"AG": [1.0, 1.0, 0.0, 1.0, 1.0, 1.0]}, index=dates)

    panel = build_causal_remaining_horizon_panel(weights)

    assert panel.loc[pd.Timestamp("2026-01-08"), "AG"] == 2.0
    assert panel.loc[pd.Timestamp("2026-01-09"), "AG"] == 1.0
    assert panel.loc[pd.Timestamp("2026-01-12"), "AG"] == 1.0


def test_horizon_panel_is_parameter_free_and_at_least_one_for_active_targets():
    from afuture.directional_shadow_lifecycle import build_causal_remaining_horizon_panel

    dates = pd.bdate_range("2026-01-05", periods=8)
    weights = pd.DataFrame(
        {"AG": [1, 1, 0, -1, -1, 0, 1, 1], "CU": [0, 1, 1, 1, 0, 0, -1, -1]},
        index=dates,
        dtype=float,
    )
    panel = build_causal_remaining_horizon_panel(weights)
    active = weights.abs() > 0

    assert (panel.where(active).stack() >= 1.0).all()
    assert (panel.where(~active).fillna(1.0) == 1.0).all().all()
