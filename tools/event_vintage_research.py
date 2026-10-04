"""Prepare strictly captured official supply vintages and qualify replay readiness."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from adaptive_alpha_research import write_json
from usda_revision_research import capture, entry_date_after_archive, parse_report, revision_rows

from afuture.event_vintages import captured_version_available_at


def prepare_event_vintages(prior, output, calendar, *, refresh=False):
    """Preserve observed time per byte version; stale archives cannot become early events."""
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((prior / "FILES.json").read_text())["files"]
    folder = prior / "U1_inputs"
    metas = sorted(folder.glob("txt_*.json"))
    if not metas:
        raise ValueError("missing preserved official report captures")
    items = [(json.loads(path.read_text()), folder, path) for path in metas]
    if refresh:
        last = items[-1][0]
        refreshed = capture(last["url"], output, "latest_known_official_version")
        refreshed["report_date"] = last["id"].removeprefix("txt_")
        items.append((refreshed, output, output / "latest_known_official_version.json"))
    rows, panel, identities = [], [], []
    days = [pd.Timestamp(day).date().isoformat() for day in calendar]
    for meta, base, meta_path in items:
        body_path = base / meta["file"]
        for path in (meta_path, body_path):
            if base == folder:
                relative = path.relative_to(prior).as_posix()
                expected = manifest[relative]
                raw = path.read_bytes()
                if (
                    len(raw) != expected["bytes"]
                    or hashlib.sha256(raw).hexdigest() != expected["sha256"]
                ):
                    raise ValueError("preserved official input changed")
                identities.append({"path": str(path), "sha256": expected["sha256"]})
        payload = body_path.read_bytes()
        report_date = meta.get("report_date", meta["id"].removeprefix("txt_"))
        try:
            available = captured_version_available_at(meta, payload)
            entry = entry_date_after_archive(available.isoformat(), days)
            parsed = parse_report(payload.decode("utf-8"), report_date)
            for row in parsed:
                row["source_sha256"], row["available_at"] = meta["sha256"], available.isoformat()
            panel.extend(parsed)
            rows.append(
                {
                    "report_date": report_date,
                    "sha256": meta["sha256"],
                    "available_at": available.isoformat(),
                    "eligible_historical_entry": entry,
                    "status": "captured_version_only",
                    "original_publication_certified": False,
                }
            )
        except (ValueError, UnicodeDecodeError, KeyError) as error:
            rows.append(
                {
                    "report_date": report_date,
                    "sha256": meta["sha256"],
                    "status": "invalid_evidence",
                    "error": str(error),
                }
            )
    pd.DataFrame(rows).to_csv(output / "qualification.csv", index=False)
    # Multiple captures of a release are versions, not duplicate releases in a revision fit.
    write_json(output / "parsed_versions.json", panel)
    valid_dates = sorted(
        set(row["report_date"] for row in rows if row["status"] == "captured_version_only")
    )
    versions = {}
    for row in panel:
        key = (row["release_date"], row["commodity"], row["crop_year"], row["role"])
        versions.setdefault(key, row)
    original_panel = list(versions.values())
    write_json(output / "revision_diagnostic.json", revision_rows(original_panel))
    qualification = {
        "status": "needs_prospective_qualified_event_observations",
        "historical_account_ready": False,
        "successful_version_captures": sum(
            row["status"] == "captured_version_only" for row in rows
        ),
        "distinct_report_dates": len(valid_dates),
        "historical_entries_after_actual_capture": sum(
            bool(row.get("eligible_historical_entry")) for row in rows
        ),
        "availability_rule": "these exact bytes only after completed public capture; never document/HTTP production time",
        "inputs": identities,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "required_next": "two timely adjacent new official report captures plus genuinely later market outcomes; archive revisions cannot be backdated",
        "scheduler_installed": False,
    }
    if qualification["historical_entries_after_actual_capture"]:
        qualification["status"] = "requires_registered_freshness_and_revision_event_recipe"
    write_json(output / "SUMMARY.json", qualification)
    return qualification


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--calendar", required=True, type=Path)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    calendar = pd.read_csv(args.calendar, index_col=0).index
    print(
        json.dumps(prepare_event_vintages(args.prior, args.output, calendar, refresh=args.refresh))
    )
