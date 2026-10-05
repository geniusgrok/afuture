"""Engineering fixtures for causal calibration and owned minute cash only."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import cross_session_research as research

SHA = "a" * 64


def bars(day="2026-09-16", price=700.0):
    rows = []
    for clock in research.EXPECTED_5M:
        end = research.stamp(day, clock)
        rows.append(
            {
                "bar_start_utc": end - pd.Timedelta(minutes=5),
                "bar_end_utc": end,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 20.0,
                "source_sha256": SHA,
            }
        )
    return pd.DataFrame(rows)


def predictions(day="2026-09-16", forecast=0.01):
    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp(day),
                "decision": research.stamp(day, "09:30:01"),
                "entry_at": research.stamp(day, research.ENTRY),
                "exit_at": research.stamp(day, research.EXIT),
                "forecast": forecast,
                "forecast_status": "mature_prediction",
            }
        ]
    )


def observations(count=27):
    rows = []
    for i, day in enumerate(pd.bdate_range("2026-08-03", periods=count)):
        x = 0.02 + i * 0.0001
        response = 0.5 * x - 0.001 * (1 + i % 3)
        row = {
            "date": day,
            "closed_at": research.stamp(day, "02:30"),
            "decision": research.stamp(day, "09:30:01"),
            "response_available": research.stamp(day, "09:15") + research.LAG,
            "x": x,
            "y": response,
            "status": "qualified_observations",
            "target": 0.002 + i * 0.0001,
            "target_matured_at": research.stamp(day, "14:50"),
        }
        definitions = (
            ("foreign_start", 100, "02:30"),
            ("foreign_end", 100 * np.exp(x), "09:00"),
            ("fx_start", 7, "02:30"),
            ("fx_end", 7, "09:00"),
            ("domestic_start", 700, "02:30"),
            ("domestic_response", 700 * np.exp(response), "09:15"),
        )
        for name, value, clock in definitions:
            row.update(
                {
                    name + "_value": value,
                    name + "_measured": research.stamp(day, clock),
                    name + "_available": research.stamp(day, clock) + research.LAG,
                    name + "_sha256": SHA,
                }
            )
        rows.append(row)
    return pd.DataFrame(rows)


def test_minimum_twelve_and_strict_maturity_before_response_beta():
    frame = observations(14)
    prediction, audit = research.forecasts(frame, "XS1")
    assert prediction.iloc[:12].forecast.isna().all()
    assert np.isfinite(prediction.iloc[12].forecast)
    assert audit.iloc[12].response_dates == 12
    frame.loc[11, "response_available"] = frame.loc[12, "decision"]
    prediction, _ = research.forecasts(frame, "XS1")
    assert prediction.iloc[12].forecast_status == "insufficient_mature_response_dates"


def test_remaining_return_model_requires_twelve_ex_ante_mature_residuals():
    frame = observations()
    predicted, audit = research.forecasts(frame, "XS2")
    assert predicted.iloc[:24].forecast.isna().all()
    assert np.isfinite(predicted.iloc[24].forecast)
    assert audit.iloc[24].remaining_dates == 12
    assert audit.iloc[24].remaining_max_maturity < frame.iloc[24].decision
    # Current and future outcomes may exist in the file but cannot affect a signal.
    changed = frame.copy()
    changed.loc[24:, "target"] = 100
    changed.loc[25:, "x"] = 100
    other, _ = research.forecasts(changed, "XS2")
    assert other.iloc[24].forecast == predicted.iloc[24].forecast
    missing = frame.copy()
    missing.loc[24, "target"] = np.nan
    other, _ = research.forecasts(missing, "XS2")
    assert other.iloc[24].forecast == predicted.iloc[24].forecast


def test_closure_excludes_the_nine_oclock_start_bar_future_close():
    day = "2026-09-16"
    records = []
    for clock, value in (("02:35", 70), ("09:00", 71), ("09:05", 999)):
        end = research.stamp(day, clock)
        records.append(
            {
                "bar_start_utc": end - pd.Timedelta(minutes=5),
                "bar_end_utc": end,
                "open": value,
                "high": value,
                "low": value,
                "close": value,
            }
        )
    foreign = pd.DataFrame(records)
    fx = foreign.copy()
    fx[["open", "high", "low", "close"]] = 7.0
    _, end, _, end_fx = research.select_closure(
        foreign, fx, research.stamp(day, "02:30"), research.stamp(day, "09:00")
    )
    assert end.close == 71
    assert end.bar_end_utc == end_fx.bar_end_utc == research.stamp(day, "09:00")


def test_owned_minute_prices_full_costs_and_independent_cash():
    source = bars()
    exit_at = research.stamp("2026-09-16", research.EXIT)
    source.loc[source.bar_start_utc >= exit_at, ["open", "high", "low", "close"]] = 703
    ledger, events, decisions, minute_dd = research.replay(
        source, predictions(), [pd.Timestamp("2026-09-16")], cost_bps=15, margin=0.15
    )
    assert len(events.loc[events.action.eq("entry")]) == 1
    assert events.loc[events.action.eq("entry"), "price"].item() == 700
    assert events.loc[events.action.eq("exit"), "price"].item() == 703
    assert ledger.iloc[-1].equity == pytest.approx(500000 + 3000 - (700 + 703) * 1000 * 0.0015)
    assert minute_dd >= 1050 / 500000
    assert decisions.iloc[0].reason == "accepted"
    verified = research.verify_cash(source, ledger, events, 15)
    assert verified["ending_lots"] == 0
    altered = events.copy()
    altered.loc[0, "transaction_cost"] = 0
    with pytest.raises(ValueError, match="cash differs"):
        research.verify_cash(source, ledger, altered, 15)


def test_missing_held_bar_is_invalid_and_partial_cash_is_preserved(tmp_path):
    source = bars()
    source = source.loc[source.bar_end_utc != research.stamp("2026-09-16", "10:00")]
    with pytest.raises(ValueError, match="missing held"):
        research.replay(
            source,
            predictions(),
            [pd.Timestamp("2026-09-16")],
            cost_bps=15,
            margin=0.15,
            failure_output=tmp_path,
        )
    assert (tmp_path / "partial_events.csv").exists()
    assert pd.read_csv(tmp_path / "partial_events.csv").iloc[0].action == "entry"
    assert (tmp_path / "partial_state.json").exists()


def test_same_time_future_signal_and_integer_margin_capacity():
    forecast = predictions()
    forecast.loc[0, "decision"] = forecast.loc[0, "entry_at"]
    with pytest.raises(ValueError, match="strictly after"):
        research.replay(bars(), forecast, [pd.Timestamp("2026-09-16")], cost_bps=15, margin=0.15)
    ledger, events, decisions, _ = research.replay(
        bars(price=1000), predictions(), [pd.Timestamp("2026-09-16")], cost_bps=15, margin=0.15
    )
    assert events.empty
    assert ledger.iloc[0].equity == 500000
    assert decisions.iloc[0].reason == "shared_integer_capacity"


def test_hard_drawdown_reduction_waits_for_lag_and_next_actual_open():
    source = bars()
    source.loc[
        source.bar_end_utc >= research.stamp("2026-09-16", "09:50"),
        ["open", "high", "low", "close"],
    ] = 350
    # Entry really remains at the previously registered700, then the first bar drops.
    source.loc[source.bar_start_utc == research.stamp("2026-09-16", "09:45"), "open"] = 700
    ledger, events, decisions, _ = research.replay(
        source, predictions(), [pd.Timestamp("2026-09-16")], cost_bps=15, margin=0.15
    )
    actual_exit = events.loc[events.action.eq("exit"), "timestamp"].item()
    assert actual_exit == research.stamp("2026-09-16", "10:10")
    assert actual_exit > research.stamp("2026-09-16", "09:50") + research.LAG
    assert ledger.iloc[-1].halted
    assert decisions.iloc[0].exit_reason == "hard_drawdown"
    research.verify_cash(source, ledger, events, 15)


def test_holiday_closure_uses_last_actual_afternoon_close_not_older_night_close():
    prior = "2026-09-24"
    day = "2026-09-28"
    domestic = bars(prior).iloc[[-1]].copy()
    older = domestic.copy()
    older["bar_end_utc"] = research.stamp(prior, "02:30")
    older["bar_start_utc"] = older.bar_end_utc - pd.Timedelta(minutes=15)
    response = domestic.copy()
    response["bar_end_utc"] = research.stamp(day, "09:15")
    response["bar_start_utc"] = response.bar_end_utc - pd.Timedelta(minutes=15)
    domestic = pd.concat([older, domestic, response], ignore_index=True)
    foreign = domestic.iloc[[1, 2]].copy()
    foreign.loc[foreign.index[0], "bar_end_utc"] = research.stamp(prior, "15:05")
    foreign.loc[foreign.index[1], "bar_end_utc"] = research.stamp(day, "09:00")
    foreign["bar_start_utc"] = foreign.bar_end_utc - pd.Timedelta(minutes=5)
    fx = foreign.copy()
    fx[["open", "high", "low", "close"]] = 7.0
    actual = research.build_observations(domestic, foreign, fx, bars(day))
    row = actual.loc[actual.date.eq(pd.Timestamp(day))].iloc[0]
    assert row.closed_at == research.stamp(prior, "15:00")
    assert row.foreign_start_measured >= row.closed_at
    assert row.status == "qualified_observations"


def test_vendor_monday_midnight_and_saturday_day_markers_are_not_actual_sessions():
    from afuture.runtime_calendar import RuntimeTradingCalendar

    frame = bars("2026-09-21").iloc[[0, 1]].copy()
    frame.loc[frame.index[0], "bar_end_utc"] = research.stamp("2026-09-21", "00:00")
    frame.loc[frame.index[1], "bar_end_utc"] = research.stamp("2026-09-12", "09:15")
    frame["bar_start_utc"] = frame.bar_end_utc - pd.Timedelta(minutes=15)
    qualified = research.qualify_domestic_clock(frame, RuntimeTradingCalendar.load())
    assert not qualified.physical_session_qualified.any()
    saturday_night = frame.iloc[[0]].copy()
    saturday_night["bar_end_utc"] = research.stamp("2026-09-19", "02:30")
    saturday_night["bar_start_utc"] = saturday_night.bar_end_utc - pd.Timedelta(minutes=15)
    assert research.qualify_domestic_clock(
        saturday_night, RuntimeTradingCalendar.load()
    ).physical_session_qualified.all()


def test_close_proxy_fallback_is_separate_from_actual_fill_labels_and_causal():
    frame = observations()
    frame["target_close"] = frame.target * 1.5
    frame["target_close_matured_at"] = frame.target_matured_at
    frame["target"] = np.nan
    forecast, audit = research.forecasts(frame, "XS3")
    assert forecast.iloc[:24].forecast.isna().all()
    assert np.isfinite(forecast.iloc[24].forecast)
    assert audit.iloc[24].label_kind == "fifteen_minute_close_market_proxy"
    actual_fill_variant, _ = research.forecasts(frame, "XS2")
    assert actual_fill_variant.forecast.isna().all()
    frame.loc[24:, "target_close"] = 999
    altered, _ = research.forecasts(frame, "XS3")
    assert altered.iloc[24].forecast == forecast.iloc[24].forecast
