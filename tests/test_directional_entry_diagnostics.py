import subprocess
import sys

import pandas as pd
import pytest

from afuture.directional_entry_diagnostics import (
    label_directional_entry_exit_events,
    summarize_entry_exit_quality,
)


def _events():
    return pd.DataFrame([
        {"date":"2026-01-05","kind":"trade","action":"entry","product":"A","symbol":"A2605","side":"long","lots_before":0,"lots_after":10,"delta_lots":10,"price":100.0,"turnover_notional":10000.0,"transaction_cost":15.0,"gross_pnl":0.0},
        {"date":"2026-01-07","kind":"trade","action":"exit","product":"A","symbol":"A2605","side":"long","lots_before":10,"lots_after":0,"delta_lots":-10,"price":103.0,"turnover_notional":10300.0,"transaction_cost":15.45,"gross_pnl":0.0},
        {"date":"2026-01-08","kind":"trade","action":"entry","product":"A","symbol":"A2605","side":"long","lots_before":0,"lots_after":9,"delta_lots":9,"price":104.0,"turnover_notional":9360.0,"transaction_cost":14.04,"gross_pnl":0.0},
    ])


def _contracts():
    dates = pd.bdate_range("2026-01-05", periods=12)
    return pd.DataFrame({
        "date": dates, "symbol": "A2605", "product": "A",
        "open": [100,101,102,104,105,106,107,108,109,110,111,112],
        "high": [102,103,104,106,107,108,109,110,111,112,113,114],
        "low": [99,100,101,103,104,105,106,107,108,109,110,111],
        "close": [101,102,103,105,106,107,108,109,110,111,112,113],
    })


def _history(future_scale=1.0):
    dates = pd.bdate_range("2025-11-24", "2026-01-20")
    close = 80.0 + pd.Series(range(len(dates)), dtype=float)
    frame = pd.DataFrame({
        "date": dates, "product": "A", "open": close - .5,
        "high": close + 1, "low": close - 1, "close": close,
        "volume": 10000.0,
    })
    mask = frame["date"] >= pd.Timestamp("2026-01-05")
    frame.loc[mask, ["open", "high", "low", "close"]] *= future_scale
    return frame


def _labeled(events=None):
    return label_directional_entry_exit_events(
        events=_events() if events is None else events,
        specific_contracts=_contracts(),
        product_history=_history(),
        horizons=(1, 3, 5),
        stress_cost_bps=15.0,
    )


def test_future_rows_change_labels_not_completed_history_features():
    baseline = _labeled()
    changed = label_directional_entry_exit_events(
        events=_events(), specific_contracts=_contracts(),
        product_history=_history(future_scale=2.0), horizons=(1,3,5), stress_cost_bps=15.0,
    )
    features = [name for name in baseline if name.startswith("feature_")]
    assert baseline.iloc[0][features].to_dict() == changed.iloc[0][features].to_dict()
    assert baseline.iloc[0]["label_gross_return_h3"] == pytest.approx(.03)
    assert baseline.iloc[0]["label_net_return_h3"] == pytest.approx(.027)
    assert baseline.iloc[0]["label_mfe_h3"] == pytest.approx(.04)
    assert baseline.iloc[0]["label_mae_h3"] == pytest.approx(-.01)


def test_rapid_reentry_is_causal_only_for_the_later_entry():
    labeled = _labeled()
    exit_row = labeled[labeled.event_role == "exit"].iloc[0]
    entry = labeled[(labeled.event_role == "entry") & (labeled.date == pd.Timestamp("2026-01-08"))].iloc[0]
    assert bool(exit_row["label_rapid_same_side_reentry_within_3"])
    assert int(exit_row["label_same_side_reentry_delay_sessions"]) == 1
    assert bool(exit_row["label_temporary_displacement_h5"])
    assert int(entry["feature_sessions_since_same_side_exit"]) == 1
    assert bool(entry["feature_is_rapid_reentry"])


def test_genuine_reversal_is_separate_from_same_side_reentry():
    events = _events().iloc[:2].copy()
    events = pd.concat([events, pd.DataFrame([{
        "date":"2026-01-08","kind":"trade","action":"reversal","product":"A","symbol":"A2605","side":"short",
        "lots_before":0,"lots_after":-9,"delta_lots":-9,"price":104.0,"turnover_notional":9360.0,"transaction_cost":14.04,"gross_pnl":0.0,
    }])], ignore_index=True)
    exit_row = _labeled(events)[lambda x: x.event_role == "exit"].iloc[0]
    assert bool(exit_row["label_genuine_reversal_within_1"])
    assert not bool(exit_row["label_rapid_same_side_reentry_within_3"])


def test_future_holding_duration_is_label_not_feature():
    first = _labeled()[lambda x: x.event_role == "entry"].iloc[0]
    assert int(first["label_holding_sessions_to_exit"]) == 2
    assert "feature_holding_sessions_to_exit" not in first.index


def test_exit_position_age_is_completed_history_feature():
    exit_row = _labeled()[lambda x: x.event_role == "exit"].iloc[0]
    assert int(exit_row["feature_sessions_since_same_side_entry"]) == 2
    assert bool(exit_row["feature_is_short_hold_exit"])
    assert "label_sessions_since_same_side_entry" not in exit_row.index


def test_summary_separates_causal_cohorts_from_future_labels():
    summary = summarize_entry_exit_quality(_labeled(), horizons=(1,3,5))
    assert summary["entries"]["count"] == 2
    assert summary["exits"]["count"] == 1
    assert summary["entries"]["causal_cohorts"]["rapid_reentry"]["count"] == 1
    assert summary["entries"]["causal_cohorts"]["fresh_entry"]["count"] == 1
    assert summary["entries"]["horizons"]["5"]["available_count"] == 2
    assert summary["entries"]["horizons"]["5"]["win_rate_after_cost"] == pytest.approx(1.0)
    assert summary["exits"]["future_only_labels"]["temporary_displacement_h5"]["count"] == 1
    assert summary["meta_cause_reliably_reconstructable"] is False


def test_exit_summary_uses_completed_position_age():
    summary = summarize_entry_exit_quality(_labeled(), horizons=(1,3,5))
    assert summary["exits"]["causal_cohorts"]["short_hold_exit_le_3"]["count"] == 1
    assert summary["exits"]["causal_cohorts"]["established_exit_gt_3"]["count"] == 0


def test_diagnostic_cli_help_is_available():
    result = subprocess.run(
        [sys.executable, "tools/analyze_directional_entry_quality.py", "--help"],
        text=True, capture_output=True,
    )
    assert result.returncode == 0
    assert {"--events", "--specific-contracts", "--product-history"} <= set(result.stdout.split())
