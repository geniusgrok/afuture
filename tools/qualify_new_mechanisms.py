"""Qualify preserved public probes; never turn current captures into old signals."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np
import pandas as pd
from adaptive_alpha_research import write_json
from minute_session_research import daytime_ends

from afuture.runtime_calendar import RuntimeTradingCalendar


def captured(folder: Path, name: str):
    metadata = json.loads((folder / (name + ".json")).read_text())
    raw = (folder / metadata["file"]).read_bytes()
    if len(raw) != metadata["bytes"] or hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
        raise ValueError("public capture bytes differ")
    if metadata.get("http_status") != 200:
        raise ValueError("public capture was unsuccessful")
    return metadata, raw


def jsonp(raw: bytes):
    text = raw.decode("utf-8")
    match = re.search(r"var\s+_afuture\s*=\s*\((.*)\);\s*$", text, re.S)
    if match is None:
        raise ValueError("unexpected quote wrapper")
    return json.loads(match.group(1))


def archived_byte_proof(records, metadata, raw):
    """Bind actual archived payload to its CDX digest and Memento clock.

    This proves availability no later than that crawl, not at original release.
    It does not supply earlier adjacent vintages or mature event calibration.
    """
    if metadata.get("http_status") != 200:
        raise ValueError("archive payload unavailable")
    if len(raw) != metadata["bytes"] or hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
        raise ValueError("archive capture bytes changed")
    headers = {key.lower(): value for key, value in metadata["headers"].items()}
    clock = parsedate_to_datetime(headers["memento-datetime"])
    if clock.tzinfo is None:
        raise ValueError("archive clock lacks timezone")
    digest = base64.b32encode(hashlib.sha1(raw).digest()).decode().rstrip("=")
    matches = []
    for row in records[1:]:
        record = dict(zip(records[0], row, strict=True))
        at = datetime.strptime(record["timestamp"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        if record["statuscode"] == "200" and record["digest"] == digest and at == clock:
            expected = (
                "https://web.archive.org/web/" + record["timestamp"] + "id_/" + record["original"]
            )
            if metadata["response_url"] != expected or metadata["url"] != expected:
                raise ValueError("archive URL/clock/source identity mismatch")
            matches.append(record)
    observed = datetime.fromisoformat(metadata["observed_at"])
    if len(matches) != 1 or observed.tzinfo is None or observed < clock:
        raise ValueError("archive digest/clock not uniquely bound")
    return {
        "archive_available_no_later_than": clock.isoformat(),
        "source_sha256": metadata["sha256"],
        "archived_original_url": matches[0]["original"],
        "actual_retrieved_at": metadata["observed_at"],
        "original_publication_time_certified": False,
        "byte_digest_and_memento_bound": True,
    }


def run(root: Path, prior: Path, output: Path | None = None):
    captures = root / "acquisition"
    output = output or root / "qualified_inputs"
    output.mkdir(exist_ok=False)
    boundary = pd.Timestamp("2026-09-30 15:00", tz="Asia/Shanghai")
    events = pd.read_csv(
        prior / "afuture-strategy-routing-20261004/event_vintages_latest/qualification.csv"
    )
    event_eligible = int((pd.to_datetime(events.available_at, utc=True) < boundary).sum())
    inventory = json.loads(
        (prior / "afuture-adaptive-alpha-20261004/supply_versions.json").read_text()
    )
    inventory_eligible = sum(pd.Timestamp(row["available_at"]) < boundary for row in inventory)
    reports = {
        "EVR": {
            "status": "blocked",
            "observed_versions": len(events),
            "versions_available_before_history_end": event_eligible,
            "reason": "exact versions captured after historical price end; no qualified event-response calibration",
            "required": [
                "timely version plus preceding vintage/actual consensus",
                "post-availability response prices",
                "twelve mature earlier event outcomes",
            ],
            "implemented": "causal residual calculation; executable recipe still requires a fitted qualified calibration",
        },
        "FUND_BFL": {
            "status": "blocked",
            "inventory_rows": len(inventory),
            "inventory_rows_available_before_history_end": inventory_eligible,
            "reason": "no historical available inventory/spot/carry fair-curvature anchor",
            "required": [
                "qualified historical inventory/warehouse version",
                "causal spot/carry fair curvature",
                "three concrete legs",
            ],
            "fallbacks": ["BFL1", "BFL2", "BFL2_EXFGJM"],
        },
    }
    calendar = RuntimeTradingCalendar.load()
    domestic = {}
    for product in ("SC", "M"):
        name = "domestic_" + product + "2701_minute"
        metadata, raw = captured(captures, name)
        frame = pd.DataFrame(jsonp(raw)).rename(
            columns={
                "d": "datetime",
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
                "p": "hold",
            }
        )
        frame["datetime"] = pd.to_datetime(frame.datetime)
        frame["symbol"], frame["product"] = product + "2701", product
        numeric = ["open", "high", "low", "close", "volume", "hold"]
        frame[numeric] = frame[numeric].astype(float)
        if frame.datetime.duplicated().any() or not np.isfinite(frame[numeric]).all().all():
            raise ValueError("ambiguous/nonfinite domestic minute quotes")
        if (
            (frame[["open", "high", "low", "close"]] <= 0).any().any()
            or (frame.high < frame[["open", "close"]].max(axis=1)).any()
            or (frame.low > frame[["open", "close"]].min(axis=1)).any()
        ):
            raise ValueError("domestic OHLC evidence invalid")
        frame.to_csv(output / (product + "2701.csv"), index=False)
        first, last = frame.datetime.min().normalize(), frame.datetime.max().normalize()
        exchange = calendar.products[product].exchange
        days = [
            pd.Timestamp(day)
            for day in calendar.open_days[exchange]
            if first.date() < day <= last.date()
        ]
        missing = {
            str(day.date()): int((~daytime_ends(day).isin(frame.datetime)).sum())
            for day in days
            if not daytime_ends(day).isin(frame.datetime).all()
        }
        domestic[product] = {
            "rows": len(frame),
            "first": str(frame.datetime.min()),
            "last": str(frame.datetime.max()),
            "complete_subsequent_daytime_sessions": not bool(missing),
            "missing_bars_by_day": missing,
            "source_sha256": metadata["sha256"],
            "timezone_rule": "domestic exchange Asia/Shanghai; five-minute end stamps",
        }
    foreign_metadata, foreign_raw = captured(captures, "foreign_CL_minute_probe")
    foreign = jsonp(foreign_raw)["minLine_1d"]
    foreign_dates = sorted({row[-1].split()[0] for row in foreign})
    fx_metadata, fx_raw = captured(captures, "fx_USDCNY_minute")
    fx = pd.DataFrame(jsonp(fx_raw))
    fx.to_csv(output / "USDCNY_unqualified_clocks.csv", index=False)
    reports["XSESSION"] = {
        "status": "blocked",
        "domestic": domestic,
        "foreign_rows": len(foreign),
        "foreign_dates": foreign_dates,
        "fx_rows": len(fx),
        "fx_first": fx.d.iloc[0],
        "fx_last": fx.d.iloc[-1],
        "foreign_source_sha256": foreign_metadata["sha256"],
        "fx_source_sha256": fx_metadata["sha256"],
        "joined_qualified_reopenings": 0,
        "reason": "current foreign minutes do not overlap domestic history; CL endpoint supplies no concrete expiry identity or certified historical timezone mapping",
        "required": [
            "concrete historical foreign minutes",
            "certified quote clocks and closure map",
            "FX and actual domestic reopening response",
            "mature paired mapping calibration",
        ],
    }
    meta, raw = captured(captures, "option_PG2611_chain")
    chain = json.loads(raw)["result"]["data"]
    options = []
    for kind, values in chain.items():
        for row in values:
            options.append(
                {
                    "kind": kind,
                    "symbol": row[-1],
                    "strike": re.fullmatch(r"pg2611[CP](\d+)", row[-1]).group(1),
                    "bid_volume": row[0],
                    "bid": row[1],
                    "last": row[2],
                    "ask": row[3],
                    "ask_volume": row[4],
                    "hold": row[5],
                    "available_at": meta["observed_at"],
                    "source_sha256": meta["sha256"],
                }
            )
    option_frame = pd.DataFrame(options)
    for name in ("bid", "ask", "bid_volume", "ask_volume"):
        option_frame[name] = pd.to_numeric(option_frame[name], errors="coerce")
    valid = option_frame.loc[
        (option_frame.bid > 0)
        & (option_frame.ask >= option_frame.bid)
        & (option_frame.bid_volume > 0)
        & (option_frame.ask_volume > 0)
    ]
    option_frame.to_csv(output / "PG2611_snapshot.csv", index=False)
    _, history_raw = captured(captures, "option_PG2611C4200_daily")
    history = jsonp(history_raw)
    reports["OPTION_BUY"] = {
        "status": "blocked",
        "snapshot_contracts": len(options),
        "positive_two_sided_snapshots": len(valid),
        "snapshot_available_at": meta["observed_at"],
        "historical_probe_days": len(history),
        "historical_bid_ask_pairs": 0,
        "mature_option_resale_bid_outcomes": 0,
        "reason": "current chain has no historical executable bid/ask sequence or verified exercise cutoff; historical probe supplies daily OHLC only",
        "required": [
            "dated concrete historical bid/ask with sizes",
            "mature option resale-bid distribution",
            "verified multiplier/fee/exercise metadata",
            "complete owned-premium ledger and pre-exercise exit evidence",
        ],
    }
    write_json(output / "QUALIFICATION.json", reports)
    print(
        json.dumps(
            {
                name: {"status": value["status"], "reason": value["reason"]}
                for name, value in reports.items()
            }
        )
    )
    return reports


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run(args.root, args.prior, args.output)
