"""Money and causal-input invariants; fixtures are engineering evidence only."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from afuture.new_mechanisms import (
    Observation,
    anchored_curve_residual,
    bought_option_edge,
    bought_option_net,
    butterfly_lots,
    cross_session_residual,
    event_residual,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import new_mechanism_research as research
from qualify_new_mechanisms import archived_byte_proof

D = datetime(2026, 9, 11, 2, tzinfo=timezone.utc)


def observed(value, measured, available=None):
    return Observation(value, measured, available or measured, "a" * 64)


def test_event_residual_subtracts_observed_response_and_rejects_backdating():
    event = observed(0.1, D - timedelta(hours=3), D - timedelta(hours=2))
    response = observed(0.03, D - timedelta(minutes=5))
    kwargs = dict(
        response_started_at=D - timedelta(hours=1),
        decision=D,
        response_per_shock=0.5,
        calibration_matured_at=D - timedelta(days=1),
        calibration_events=12,
    )
    assert event_residual(event, response, **kwargs) == pytest.approx(0.02)
    captured_late = observed(0.1, event.measured_at, D + timedelta(days=1))
    with pytest.raises(ValueError, match="strictly before"):
        event_residual(captured_late, response, **kwargs)
    with pytest.raises(ValueError, match="precedes"):
        event_residual(event, response, **{**kwargs, "response_started_at": D - timedelta(hours=4)})
    with pytest.raises(ValueError, match="twelve"):
        event_residual(event, response, **{**kwargs, "calibration_matured_at": D})


def test_fundamental_anchor_cannot_be_a_late_capture_or_unequal_integer_butterfly():
    prices = tuple(observed(p, D - timedelta(hours=1)) for p in (102, 101, 104))
    assert anchored_curve_residual(observed(5, D - timedelta(days=1)), prices, D) == 1
    with pytest.raises(ValueError, match="strictly before"):
        anchored_curve_residual(observed(5, D, D), prices, D)
    assert butterfly_lots((24290, 24294, 24298)) == (1, -2, 1)
    with pytest.raises(ValueError, match="equally spaced"):
        butterfly_lots((24290, 24293, 24298))


def test_cross_session_uses_residual_after_domestic_response():
    closed, opened = D - timedelta(hours=8), D - timedelta(minutes=30)
    foreign = (observed(100, closed), observed(110, opened))
    fx = (observed(7, closed), observed(7, opened))
    domestic = (observed(100, closed), observed(105, opened + timedelta(minutes=5)))
    kwargs = dict(
        decision=D,
        closure_started_at=closed,
        domestic_opened_at=opened,
        beta=1,
        calibration_matured_at=D - timedelta(days=1),
    )
    assert cross_session_residual(foreign, fx, domestic, **kwargs) == pytest.approx(
        np.log(110 / 105)
    )
    with pytest.raises(ValueError, match="inside"):
        cross_session_residual(
            (foreign[0], observed(110, D - timedelta(minutes=1))), fx, domestic, **kwargs
        )
    with pytest.raises(ValueError, match="align"):
        cross_session_residual(foreign, fx, (domestic[0], observed(105, closed)), **kwargs)


def test_bought_options_use_paid_ask_resale_bid_and_exit_before_exercise():
    kwargs = dict(
        ask=observed(10, D - timedelta(minutes=1)),
        expected_resale_bid=13,
        calibration_matured_at=D - timedelta(days=1),
        decision=D,
        planned_exit=D + timedelta(days=3),
        exercise_cutoff=D + timedelta(days=5),
        multiplier=10,
        roundtrip_fees=4,
    )
    assert bought_option_edge(**kwargs) == 26
    assert bought_option_net(10, 13, 10, 2, 8) == 52
    with pytest.raises(ValueError, match="exercise"):
        bought_option_edge(**{**kwargs, "planned_exit": kwargs["exercise_cutoff"]})
    with pytest.raises(ValueError, match="integer"):
        bought_option_net(10, 13, 10, 1.5, 0)


def small_market():
    days = pd.date_range("2026-08-03", periods=8, freq="B")
    rows = []
    for i, day in enumerate(days):
        for symbol, delivery, p in (
            ("M2701", "2027-01-15", 110 - i),
            ("M2705", "2027-05-15", 100 + i),
            ("M2709", "2027-09-15", 110 - i),
        ):
            rows.append(
                dict(
                    date=day,
                    delivery=pd.Timestamp(delivery),
                    symbol=symbol,
                    product="M",
                    volume=10000,
                    hold=10000,
                    open=p,
                    close=p + 0.5,
                )
            )
    return pd.DataFrame(rows), days


def test_butterfly_whole_legs_share_one_cash_account_with_actual_costs():
    market, days = small_market()
    rows = research.observations(market)
    predicted, _ = research.forecasts(rows, days, "BFL1", "full")
    ledger, events, _ = research.replay(
        market, predicted, days[1:], {"M": 10}, cost_bps=15, margin=0.15, mechanism="BFL1"
    )
    assert len(events) > 0
    entry = events.loc[events.action == "entry"].head(3)
    assert entry.lots.tolist() == [-1, 2, -1]
    checked = research.verify_account(market, ledger, events, {"M": 10}, 15)
    assert checked["cash_error"] < 1e-6
    assert ledger.equity.iloc[-1] == pytest.approx(500000 + events.pnl.sum() - events.fee.sum())
    assert ledger.active_baskets.iloc[-1] == 0
    corrupted = events.copy()
    corrupted.loc[0, "lots"] = 2
    with pytest.raises(AssertionError):
        research.verify_account(market, ledger, corrupted, {"M": 10}, 15)


def test_future_quotes_cannot_change_prior_selection_or_features():
    market, days = small_market()
    original = research.observations(market).iloc[0]
    modified = market.copy()
    modified.loc[(modified.date >= days[1]) & (modified.symbol == "M2701"), ["open", "close"]] *= 3
    candidate = research.observations(modified).iloc[0]
    for column in ("date", "source_day", "near", "middle", "far", "curve", "gross_points", "x1"):
        assert original[column] == candidate[column]
    assert original.target != candidate.target
    missing = research.observations(market.loc[market.date != days[5]])
    assert len(missing) > 0


def test_missing_chosen_leg_fails_evidence_instead_of_removing_losing_trade():
    market, days = small_market()
    predicted, _ = research.forecasts(research.observations(market), days, "BFL1", "full")
    missing = market.loc[~((market.date == days[2]) & (market.symbol == "M2701"))]
    with pytest.raises(ValueError, match="held concrete"):
        research.replay(
            missing, predicted, days[1:], {"M": 10}, cost_bps=15, margin=0.15, mechanism="BFL1"
        )


def test_forecast_excludes_unmatured_outcomes_and_removed_assets_before_fit():
    days = pd.date_range("2024-01-02", periods=145, freq="B")
    rows = []
    for i, day in enumerate(days):
        for product in ("M", "Y", "FG", "JM"):
            rows.append(
                dict(
                    date=day,
                    source_day=day - pd.Timedelta(days=1),
                    product=product,
                    x1=(i % 10) / 1000,
                    x2=(i % 7) / 1000,
                    maturity_day=day + pd.offsets.BDay(5),
                    target=(i % 10) / 2000,
                )
            )
    data = pd.DataFrame(rows)
    original, audit = research.forecasts(data, days[-1:], "BFL2_EXFGJM", "full")
    assert set(original["product"]) == {"M", "Y"}
    assert pd.Timestamp(audit.max_maturity.iloc[0]) < days[-1]
    assert audit.rows.iloc[0] >= 200
    altered = data.copy()
    altered.loc[
        (altered.maturity_day >= days[-1]) | altered["product"].isin(("FG", "JM")), "target"
    ] = 999
    candidate, _ = research.forecasts(altered, days[-1:], "BFL2_EXFGJM", "full")
    np.testing.assert_array_equal(candidate.forecast, original.forecast)


def test_research_entrypoint_writes_all_registered_receipts(tmp_path):
    market, _ = small_market()
    path, units = tmp_path / "market.csv", tmp_path / "units.csv"
    market.to_csv(path, index=False)
    pd.DataFrame({"product": ["M"], "account_multiplier_used": [10]}).to_csv(units, index=False)
    results = research.run(path, units, tmp_path / "result")
    assert len(results) == 24
    assert all(row["status"] == "complete" for row in results)
    assert all(row["verification"]["cash_error"] < 1e-6 for row in results)
    broken = market.loc[~((market.date == market.date.unique()[2]) & (market.symbol == "M2701"))]
    broken.to_csv(tmp_path / "broken.csv", index=False)
    continued = research.run(tmp_path / "broken.csv", units, tmp_path / "continued")
    assert any(row["status"] == "invalid_evidence" for row in continued)
    assert any(row["mechanism"] == "BFL2" and row["status"] == "complete" for row in continued)
    assert list((tmp_path / "continued").rglob("partial_state.json"))


def test_cdx_pointer_requires_matching_archived_bytes_clock_and_url():
    import base64
    import hashlib

    raw = b"engineering-only archived byte fixture"
    original = "https://esmis.nal.usda.gov/test.txt"
    url = "https://web.archive.org/web/20250110180000id_/" + original
    digest = base64.b32encode(hashlib.sha1(raw).digest()).decode().rstrip("=")
    records = [
        ["timestamp", "original", "statuscode", "digest"],
        ["20250110180000", original, "200", digest],
    ]
    metadata = {
        "http_status": 200,
        "url": url,
        "response_url": url,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "observed_at": "2026-10-05T01:00:00+00:00",
        "headers": {"Memento-Datetime": "Fri, 10 Jan 2025 18:00:00 GMT"},
    }
    proof = archived_byte_proof(records, metadata, raw)
    assert proof["archive_available_no_later_than"] == "2025-01-10T18:00:00+00:00"
    assert not proof["original_publication_time_certified"]
    with pytest.raises(ValueError, match="bytes changed"):
        archived_byte_proof(records, metadata, raw + b"changed")
    with pytest.raises(ValueError, match="not uniquely bound"):
        archived_byte_proof(
            records,
            {**metadata, "headers": {"Memento-Datetime": "Sat, 11 Jan 2025 18:00:00 GMT"}},
            raw,
        )
