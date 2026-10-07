"""Causal and financial boundaries of the continued offline candidates."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from continuous_rebuild_research import (
    conditional_weights,
    failure_exit,
    family_paths,
    fixed_family_weights,
    market_states,
    qualify_oi_targets,
)
from minute_session_research import daytime_ends, session_episodes


def test_failure_exit_covers_unarmed_loss_symmetrically_and_rejects_bad_evidence():
    assert failure_exit(95, 1, 100, 5, False)
    assert failure_exit(105, -1, 100, 5, False)
    assert not failure_exit(96, 1, 100, 5, False)
    assert not failure_exit(105, -1, 100, 5, True)
    with pytest.raises(ValueError):
        failure_exit(float("nan"), 1, 100, 5, False)
    with pytest.raises(ValueError):
        failure_exit(95, 1, 100, 0, False)


def test_opposing_families_cancel_without_restoring_a_gross_budget():
    index = pd.bdate_range("2024-01-01", periods=3)
    positive = pd.DataFrame({"A": [2.0] * 3}, index=index)
    negative = -positive
    assert fixed_family_weights({"trend": positive, "reversal": negative}).eq(0).all().all()
    assert fixed_family_weights({"trend": positive, "flat": positive * 0}).A.eq(1).all()


def test_current_close_cannot_change_current_family_targets_or_market_state():
    dates = pd.bdate_range("2024-01-01", periods=150)
    close = pd.DataFrame({"A": 100 * np.exp(np.arange(150) * 0.001)}, index=dates)
    old, counts = family_paths(close)
    changed = close.copy()
    changed.iloc[-1] *= 1.1
    new, new_counts = family_paths(changed)
    assert sum(counts.values()) == 96 and counts == new_counts and len(counts) == 6
    pd.testing.assert_series_equal(
        fixed_family_weights(old).iloc[-1], fixed_family_weights(new).iloc[-1]
    )
    assert market_states(close).iloc[-1] == market_states(changed).iloc[-1]


def test_conditional_selection_cannot_read_today_or_future_net_returns():
    dates = pd.bdate_range("2024-01-01", periods=80)
    states = pd.Series("high", index=dates)
    families = {"trend": pd.DataFrame({"A": 0.2}, index=dates)}
    base = pd.DataFrame({"trend": 0.01}, index=dates)
    stress = base.copy()
    old, audit = conditional_weights(families, states, base, stress)
    assert old.iloc[:60].eq(0).all().all() and old.iloc[60].A == 0.2
    stress.loc[dates[60] :, "trend"] = -1.0
    new, _ = conditional_weights(families, states, base, stress)
    assert new.iloc[60].A == old.iloc[60].A
    assert new.iloc[61].A == 0 and audit.iloc[60].prior_observations == 60


def test_missing_mandatory_oi_removes_risk_without_fabricating_flow():
    dates = pd.bdate_range("2024-01-01", periods=2)
    raw = pd.DataFrame({"TA": [0.5, 0.5], "SA": [0.5, 0.5]}, index=dates)
    flow = pd.DataFrame(1.0, index=dates, columns=["A", "C", "EG", "I", "M", "P", "PP", "TA", "Y"])
    flow.loc[dates[1], "TA"] = np.nan
    result = qualify_oi_targets(raw, flow)
    assert result.loc[dates[1], "TA"] == 0 and result.SA.eq(0.5).all()
    assert pd.isna(flow.loc[dates[1], "TA"])


def minute_fixture():
    dates = daytime_ends(pd.Timestamp("2024-01-02"))
    bars = pd.DataFrame(
        {
            "datetime": dates,
            "symbol": "A2411",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 100.0,
        }
    )
    bars.loc[3, ["close", "high"]] = [102.0, 103.0]
    bars.loc[5, ["open", "close", "high"]] = [103.0, 104.0, 105.0]
    bars.loc[8, ["open", "low"]] = [95.0, 94.0]
    return bars


def test_minute_signal_waits_a_full_bar_and_charges_both_real_quotes():
    bars = minute_fixture()
    episodes, _ = session_episodes(bars, 10.0)
    first = episodes.iloc[0]
    assert first.signal_bar_end == bars.datetime.iloc[3]
    assert first.entry_bar_end == bars.datetime.iloc[5]
    assert first.modeled_entry_time > first.signal_bar_end
    assert first.gross_pnl == -80 and first.stress_fee == pytest.approx((103 + 95) * 10 * 0.0015)


def test_minute_partial_day_is_audited_and_unpriced_exit_fails():
    bars = minute_fixture()
    episodes, audit = session_episodes(bars.iloc[20:], 10.0)
    assert episodes.empty and audit.iloc[0].reason == "incomplete45_bar_day"
    with pytest.raises(ValueError, match="incomplete subsequent session"):
        session_episodes(bars.iloc[:-1], 10.0)
    bars.loc[5, "volume"] = 0
    with pytest.raises(ValueError, match="entry lacks executable-volume"):
        session_episodes(bars, 10.0)
    bars.loc[5, "volume"] = 100
    bars.loc[8, "volume"] = 0
    with pytest.raises(ValueError, match="held episode"):
        session_episodes(bars, 10.0)
