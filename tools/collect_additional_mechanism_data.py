"""Normalize preserved cross-session quotes without inventing publication clocks.

Historical price stamps are evidence about quoted bars. They do not certify the
provider's publication latency or an executable bid/ask; those remain explicit
research assumptions in account replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afuture.runtime_calendar import RuntimeTradingCalendar

DAYTIME_ENDS = (
    "09:15",
    "09:30",
    "09:45",
    "10:00",
    "10:15",
    "10:45",
    "11:00",
    "11:15",
    "11:30",
    "13:45",
    "14:00",
    "14:15",
    "14:30",
    "14:45",
    "15:00",
)


def preserved(root: Path, name: str) -> tuple[dict, bytes]:
    meta = json.loads((root / (name + ".json")).read_text())
    raw = (root / meta["file"]).read_bytes()
    if meta.get("http_status") != 200:
        raise ValueError("unsuccessful source capture")
    if len(raw) != meta["bytes"] or hashlib.sha256(raw).hexdigest() != meta["sha256"]:
        raise ValueError("preserved source bytes changed")
    return meta, raw


def validate(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.bar_end.duplicated().any() or not frame.bar_end.is_monotonic_increasing:
        raise ValueError("duplicated or disordered historical quote clock")
    prices = frame[["open", "high", "low", "close"]]
    finite = np.isfinite(prices).all(axis=1)
    consistent = (
        (prices > 0).all(axis=1)
        & (frame.high >= prices[["open", "close"]].max(axis=1))
        & (frame.low <= prices[["open", "close"]].min(axis=1))
        & (frame.low <= frame.high)
    )
    # Null bars remain in the table. Nothing is forward-filled or fabricated.
    frame["valid_bar"] = finite & consistent
    frame["bar_end_shanghai"] = frame.bar_end.dt.tz_convert("Asia/Shanghai")
    return frame


def chart_bars(meta: dict, raw: bytes, symbol: str, minutes: int = 5) -> pd.DataFrame:
    chart = json.loads(raw)["chart"]
    if chart["error"] is not None or len(chart["result"] or []) != 1:
        raise ValueError("historical chart unavailable or ambiguous")
    result = chart["result"][0]
    facts = result["meta"]
    expected_type = "FUTURE" if symbol.endswith(".NYM") else "CURRENCY"
    expected_zone = "America/New_York" if expected_type == "FUTURE" else "Europe/London"
    if (
        facts["symbol"] != symbol
        or facts["instrumentType"] != expected_type
        or facts["exchangeTimezoneName"] != expected_zone
        or facts["dataGranularity"] != f"{minutes}m"
    ):
        raise ValueError("concrete symbol/type/granularity/timezone mismatch")
    frame = pd.DataFrame(result["indicators"]["quote"][0])
    frame["bar_start"] = pd.to_datetime(result["timestamp"], unit="s", utc=True)
    frame["bar_end"] = frame.bar_start + pd.Timedelta(minutes=minutes)
    frame["symbol"] = symbol
    frame["source_sha256"] = meta["sha256"]
    frame["actual_captured_at"] = meta["observed_at"]
    frame["historical_publication_latency_certified"] = False
    return validate(frame)


def domestic_session_mask(frame: pd.DataFrame) -> pd.Series:
    """Keep source anomalies but forbid using physically closed-session stamps."""
    calendar = RuntimeTradingCalendar.load()
    mask = []
    for _, row in frame.iterrows():
        # Session windows exclude their exact end. Test immediately inside each
        # boundary, so actual 02:30/15:00 closing bars remain eligible.
        ended = calendar.expected_trading_day(
            "SC2701", (row.bar_end - pd.Timedelta(microseconds=1)).to_pydatetime()
        )
        started = (
            calendar.expected_trading_day(
                "SC2701", (row.bar_start + pd.Timedelta(microseconds=1)).to_pydatetime()
            )
            if "bar_start" in frame.columns
            else ended
        )
        mask.append(ended is not None and started == ended)
    return pd.Series(mask, index=frame.index, dtype=bool)


def domestic_bars(meta: dict, raw: bytes, symbol: str, minutes: int) -> pd.DataFrame:
    match = re.search(r"var\s+_afuture\s*=\s*\((.*)\);\s*$", raw.decode(), re.S)
    if match is None:
        raise ValueError("unexpected domestic wrapper")
    frame = pd.DataFrame(json.loads(match.group(1))).rename(
        columns={
            "d": "bar_end",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
            "p": "hold",
        }
    )
    frame["bar_end"] = (
        pd.to_datetime(frame.bar_end).dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")
    )
    frame["bar_start"] = frame.bar_end - pd.Timedelta(minutes=minutes)
    for name in ("open", "high", "low", "close", "volume", "hold"):
        frame[name] = pd.to_numeric(frame[name], errors="raise")
    frame["symbol"] = symbol
    frame["source_sha256"] = meta["sha256"]
    frame["actual_captured_at"] = meta["observed_at"]
    frame["historical_publication_latency_certified"] = False
    frame = validate(frame)
    frame["session_clock_eligible"] = domestic_session_mask(frame)
    return frame


def clock_check(five: pd.DataFrame, one: pd.DataFrame) -> dict:
    minute = one.set_index("bar_start")
    aggregate = minute.resample("5min", label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "valid_bar": "sum",
        }
    )
    joined = aggregate.join(five.set_index("bar_start"), lsuffix="_1m", rsuffix="_5m")
    joined = joined.loc[(joined.valid_bar_1m == 5) & joined.valid_bar_5m.fillna(False)]
    if joined.empty:
        raise ValueError("no fully observed one-minute aggregation checks")
    price_error = max(
        float((joined[name + "_1m"] - joined[name + "_5m"]).abs().max())
        for name in ("open", "high", "low", "close")
    )
    return {
        "complete_one_minute_groups": len(joined),
        "max_ohlc_discrepancy": price_error,
        "max_volume_discrepancy": float((joined.volume_1m - joined.volume_5m).abs().max()),
        "five_minute_price_start_stamp_verified_on_observed_groups": price_error == 0,
        "historical_publication_latency_certified": False,
    }


def paired_days(domestic: pd.DataFrame, foreign: pd.DataFrame, fx: pd.DataFrame) -> pd.DataFrame:
    calendar = RuntimeTradingCalendar.load()
    session_eligible = domestic_session_mask(domestic)
    lo, hi = domestic.bar_end_shanghai.min().date(), domestic.bar_end_shanghai.max().date()
    days = [day for day in calendar.open_days[calendar.products["SC"].exchange] if lo < day <= hi]
    foreign = foreign.loc[foreign.valid_bar & (foreign.volume > 0)].set_index("bar_end")
    fx_symbol = str(fx.symbol.iloc[0])
    fx = fx.loc[fx.valid_bar].set_index("bar_end")
    rows = []
    age = pd.Timedelta(minutes=15)
    for day in days:
        date = pd.Timestamp(day, tz="Asia/Shanghai")
        first = date + pd.Timedelta(hours=9, minutes=15)
        domestic_day = domestic.loc[domestic.bar_end_shanghai.dt.date == day]
        actual = set(
            domestic_day.loc[domestic_day.valid_bar, "bar_end_shanghai"].dt.strftime("%H:%M")
        )
        row = {
            "date": str(day),
            "fx_symbol": fx_symbol,
            "missing_daytime_bars": ";".join(x for x in DAYTIME_ENDS if x not in actual),
        }
        reopening = date + pd.Timedelta(hours=9)
        earlier = domestic.loc[
            (domestic.bar_end_shanghai < reopening) & domestic.valid_bar & session_eligible
        ]
        row["qualified_price_pair"] = False
        if row["missing_daytime_bars"] or earlier.empty:
            row["rejection_reason"] = "incomplete domestic session or missing prior night closure"
            rows.append(row)
            continue
        closing_bar = earlier.iloc[-1]
        if closing_bar.bar_end_shanghai.strftime("%H:%M") not in ("02:30", "15:00"):
            row["rejection_reason"] = "latest domestic observation lacks a completed closing bar"
            rows.append(row)
            continue
        closure = closing_bar.bar_end
        row["domestic_closure_end"] = closure.isoformat()
        row["domestic_response_end"] = first.isoformat()
        start = foreign.loc[(foreign.index > closure) & (foreign.index <= closure + age)]
        stop_at = (date + pd.Timedelta(hours=9)).tz_convert("UTC")
        stop = foreign.loc[(foreign.index <= stop_at) & (foreign.index >= stop_at - age)]
        if start.empty or stop.empty:
            row["rejection_reason"] = "missing fresh specific foreign closure/reopening bars"
            rows.append(row)
            continue
        f0, f1 = start.iloc[0], stop.iloc[-1]
        at0, at1 = start.index[0], stop.index[-1]
        fx0 = fx.loc[(fx.index <= at0) & (fx.index >= at0 - age)]
        fx1 = fx.loc[(fx.index >= at1) & (fx.index <= stop_at)]
        if fx0.empty or fx1.empty:
            row["rejection_reason"] = (
                "missing FX start/end coverage within frozen fifteen-minute freshness"
            )
            rows.append(row)
            continue
        x0, x1 = fx0.iloc[-1], fx1.iloc[0]
        row.update(
            qualified_price_pair=True,
            rejection_reason="",
            foreign_start=at0.isoformat(),
            foreign_end=at1.isoformat(),
            fx_start=fx0.index[-1].isoformat(),
            fx_end=fx1.index[0].isoformat(),
            foreign_price_start=float(f0.close),
            foreign_price_end=float(f1.close),
            fx_price_start=float(x0.close),
            fx_price_end=float(x1.close),
            foreign_rmb_return=float((f1.close * x1.close) / (f0.close * x0.close) - 1),
            foreign_source_sha256=f0.source_sha256,
            fx_source_sha256=x0.source_sha256,
        )
        rows.append(row)
    return pd.DataFrame(rows)


def qualify(root: Path, output: Path | None = None) -> dict:
    corrected = output is not None
    output = output or root / "normalized"
    output.mkdir(exist_ok=False)
    charts = {
        "CLF27_5m": ("concrete_CLF27_NYM_five_minute", "CLF27.NYM", 5),
        "CLX26_5m": ("concrete_CLX26_NYM_five_minute", "CLX26.NYM", 5),
        "USDCNY_5m": ("USDCNY_yahoo_august_five_minute", "CNY=X", 5),
        "USDCNH_5m": ("USDCNH_yahoo_five_minute", "CNH=X", 5),
        "CLF27_1m_clock": ("concrete_CLF27_NYM_one_minute_clock_check", "CLF27.NYM", 1),
        "USDCNH_1m_clock": ("USDCNH_one_minute_clock_check", "CNH=X", 1),
    }
    frames, report = {}, {}
    for name, (source, symbol, interval) in charts.items():
        frames[name] = chart_bars(*preserved(root, source), symbol, interval)
        frames[name].to_csv(output / (name + ".csv"), index=False)
        report[name] = {
            "rows": len(frames[name]),
            "valid_bars": int(frames[name].valid_bar.sum()),
            "zero_volume_valid_bars": int(
                (frames[name].valid_bar & (frames[name].volume == 0)).sum()
            ),
            "first_bar_end": str(frames[name].bar_end.min()),
            "last_bar_end": str(frames[name].bar_end.max()),
        }
    frames["SC2701_15m"] = domestic_bars(
        *preserved(root, "domestic_SC2701_fifteen_minute"), "SC2701", 15
    )
    frames["SC2701_15m"].to_csv(output / "SC2701_15m.csv", index=False)
    report["clock_checks"] = {
        name: clock_check(frames[name + "_5m"], frames[name + "_1m_clock"])
        for name in ("CLF27", "USDCNH")
    }
    report["pair_qualification"] = {}
    for fx in ("USDCNY", "USDCNH"):
        pairs = paired_days(frames["SC2701_15m"], frames["CLF27_5m"], frames[fx + "_5m"])
        pairs.to_csv(output / (fx + "_PAIRED_DAYS.csv"), index=False)
        count = int(pairs.qualified_price_pair.sum())
        report["pair_qualification"][fx] = {
            "calendar_days": len(pairs),
            "qualified_price_pairs": count,
            "possible_decisions_with_twelve_strictly_earlier_pairs": max(count - 12, 0),
            "rejection_reasons": pairs.loc[~pairs.qualified_price_pair]
            .rejection_reason.value_counts()
            .to_dict(),
        }
    report.update(
        status="historical_quote_research_inputs_obtained; actual provider latency and bid/ask unproven",
        source_availability="actual October capture retained; price epoch/end stamps are not report-publication certification",
        domestic_session_end_stamps=list(DAYTIME_ENDS),
        domestic_night_closure="last actual completed SC closing bar before 09:00 reopening; 02:30 or 15:00, including holiday absence of night trading",
        actual_execution_certified=False,
        published_latency_certified=False,
        prospective=False,
    )
    ((output if corrected else root) / "QUALIFICATION.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(qualify(args.root, args.output), indent=2))
