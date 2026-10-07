"""Captured-input clocks and identities; fixtures supply no economic evidence."""

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from collect_additional_mechanism_data import chart_bars, clock_check, preserved


def chart(symbol, minute, stamps, quotes):
    result = {
        "meta": {
            "symbol": symbol,
            "instrumentType": "FUTURE",
            "exchangeTimezoneName": "America/New_York",
            "dataGranularity": f"{minute}m",
        },
        "timestamp": stamps,
        "indicators": {"quote": [quotes]},
    }
    return json.dumps({"chart": {"error": None, "result": [result]}}).encode()


def meta(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "observed_at": "2026-10-05T02:00:00+00:00"}


def test_source_changed_bytes_and_failed_http_never_qualify(tmp_path):
    raw = b"original response"
    value = {**meta(raw), "bytes": len(raw), "file": "source.bin", "http_status": 200}
    (tmp_path / "source.bin").write_bytes(raw)
    (tmp_path / "source.json").write_text(json.dumps(value))
    assert preserved(tmp_path, "source")[1] == raw
    (tmp_path / "source.bin").write_bytes(b"rewritten response")
    with pytest.raises(ValueError, match="bytes changed"):
        preserved(tmp_path, "source")
    value["http_status"] = 404
    (tmp_path / "source.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="unsuccessful"):
        preserved(tmp_path, "source")


def test_concrete_quote_identity_completed_bar_and_missing_rows():
    stamp = int(pd.Timestamp("2026-09-29T09:00:00Z").timestamp())
    raw = chart(
        "CLF27.NYM",
        5,
        [stamp, stamp + 300],
        {
            "open": [90, None],
            "high": [92, None],
            "low": [89, None],
            "close": [91, None],
            "volume": [10, None],
        },
    )
    frame = chart_bars(meta(raw), raw, "CLF27.NYM")
    assert len(frame) == 2 and list(frame.valid_bar) == [True, False]
    assert frame.bar_start.iloc[0] == pd.Timestamp("2026-09-29T09:00:00Z")
    assert frame.bar_end.iloc[0] == pd.Timestamp("2026-09-29T09:05:00Z")
    assert pd.isna(frame.close.iloc[1])
    assert not frame.historical_publication_latency_certified.any()
    with pytest.raises(ValueError, match="symbol"):
        chart_bars(meta(raw), raw, "CLX26.NYM")


def test_observed_one_minute_aggregation_proves_price_bucket_but_not_latency():
    stamp = int(pd.Timestamp("2026-09-29T09:00:00Z").timestamp())
    one_raw = chart(
        "CLF27.NYM",
        1,
        [stamp + 60 * i for i in range(5)],
        {
            "open": [90, 91, 92, 93, 94],
            "high": [92, 93, 94, 95, 96],
            "low": [89, 90, 91, 92, 93],
            "close": [91, 92, 93, 94, 95],
            "volume": [1, 1, 1, 1, 1],
        },
    )
    five_raw = chart(
        "CLF27.NYM",
        5,
        [stamp],
        {
            "open": [90],
            "high": [96],
            "low": [89],
            "close": [95],
            "volume": [4],
        },
    )
    one = chart_bars(meta(one_raw), one_raw, "CLF27.NYM", 1)
    five = chart_bars(meta(five_raw), five_raw, "CLF27.NYM")
    evidence = clock_check(five, one)
    assert evidence["complete_one_minute_groups"] == 1
    assert evidence["five_minute_price_start_stamp_verified_on_observed_groups"]
    assert evidence["max_volume_discrepancy"] == 1
    assert not evidence["historical_publication_latency_certified"]
    five.loc[0, "close"] = 94
    assert not clock_check(five, one)["five_minute_price_start_stamp_verified_on_observed_groups"]


def test_holiday_reopening_uses_last_actual_domestic_close_not_prior_night():
    from collect_additional_mechanism_data import DAYTIME_ENDS, paired_days

    # 2026-09-25 is a holiday: Sep 24 15:00 follows its 02:30 night close.
    # Treating 02:30 as closure would wrongly include Sep 24's active day session.
    stamps = ["2026-09-24 02:30", "2026-09-24 15:00"] + [
        "2026-09-28 " + clock for clock in DAYTIME_ENDS
    ]
    domestic = pd.DataFrame(
        {
            "bar_end_shanghai": pd.to_datetime(stamps).tz_localize("Asia/Shanghai"),
            "valid_bar": True,
        }
    )
    domestic["bar_end"] = domestic.bar_end_shanghai.dt.tz_convert("UTC")
    endpoints = (
        pd.to_datetime(["2026-09-24 15:05", "2026-09-28 09:00"])
        .tz_localize("Asia/Shanghai")
        .tz_convert("UTC")
    )
    foreign = pd.DataFrame(
        {
            "bar_end": endpoints,
            "valid_bar": True,
            "close": [90, 91],
            "volume": [10, 11],
            "source_sha256": "a" * 64,
        }
    )
    fx = pd.DataFrame(
        {
            "bar_end": endpoints,
            "valid_bar": True,
            "close": [7, 7],
            "symbol": "CNH=X",
            "source_sha256": "b" * 64,
        }
    )
    pairs = paired_days(domestic, foreign, fx)
    assert len(pairs) == 1 and pairs.qualified_price_pair.iloc[0]
    assert pairs.domestic_closure_end.iloc[0] == "2026-09-24T07:00:00+00:00"
    assert pairs.foreign_start.iloc[0] == "2026-09-24T07:05:00+00:00"


def test_provider_monday_midnight_anomaly_is_retained_but_not_used_as_closed_period():
    from collect_additional_mechanism_data import domestic_session_mask

    ends = pd.to_datetime(
        [
            "2026-09-19T02:30:00+08:00",
            "2026-09-21T00:00:00+08:00",
            "2026-09-21T09:15:00+08:00",
        ],
        utc=True,
    )
    frame = pd.DataFrame({"bar_end": ends, "bar_start": ends - pd.Timedelta(minutes=15)})
    assert domestic_session_mask(frame).tolist() == [True, False, True]
    assert len(frame) == 3
