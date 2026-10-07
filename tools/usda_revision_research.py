"""Offline USDA world oilseed forecast vintages; never consensus surprises."""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import re
from datetime import date, datetime, time, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TITLES = {
    "soybeans": "World Soybean Supply and Use",
    "soybean_meal": "World Soybean Meal Supply and Use",
    "soybean_oil": "World Soybean Oil Supply and Use",
}
FIELDS = ["beginning_stocks", "production", "imports", "domestic_use", "exports", "ending_stocks"]
ARCHIVE = "https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates"


def parse_report(text: str, release_date: str) -> list[dict]:
    """Keep published current and restated-prior world rows as separate vintages."""
    report_date = date.fromisoformat(release_date)
    rows = []
    for commodity, title in TITLES.items():
        if text.count(title) != 1:
            raise ValueError(f"expected one table: {title}")
        section = text.split(title, 1)[1]
        section = re.split(r"\n\s*WASDE\s*-", section, maxsplit=1)[0]
        if "Million Metric Tons" not in section:
            raise ValueError("unverified units")
        crop_year = None
        lines = section.splitlines()
        for i, line in enumerate(lines):
            year = re.fullmatch(r"\s*(20\d{2}/\d{2})(?:\s+(?:Est|Proj)\.)?\s*", line)
            if year:
                crop_year = year.group(1)
            world = re.fullmatch(r"World(?:\s+\d+/)?\s*(.*)", line)
            if not world or not crop_year:
                continue
            content = world.group(1).strip()
            candidates = [("current", report_date.strftime("%Y-%m"), content)]
            if not content:
                candidates = []
                for following in lines[i + 1 : i + 3]:
                    value = re.fullmatch(r"\s*([A-Z][a-z]{2})\s+(.+)", following)
                    if not value:
                        raise ValueError("missing monthly world row")
                    month = list(calendar.month_abbr).index(value.group(1))
                    vintage_year = report_date.year - int(month > report_date.month)
                    role = "current" if month == report_date.month else "prior_as_restated"
                    candidates.append((role, f"{vintage_year:04d}-{month:02d}", value.group(2)))
            for role, vintage_month, content in candidates:
                tokens = content.split()
                expected = 7 if commodity == "soybeans" else 6
                unavailable = role == "prior_as_restated" and tokens == ["NA"] * expected
                if not unavailable and (
                    len(tokens) != expected or any(not re.fullmatch(r"\d+\.\d+", s) for s in tokens)
                ):
                    raise ValueError(f"non-numeric world data: {commodity} {crop_year} {content}")
                fields = FIELDS.copy()
                if commodity == "soybeans":
                    fields.insert(3, "crush")
                values = (
                    dict.fromkeys(fields)
                    if unavailable
                    else dict(zip(fields, map(float, tokens), strict=True))
                )
                if not unavailable and values["domestic_use"] <= 0:
                    raise ValueError("nonpositive world domestic use")
                rows.append(
                    {
                        "release_date": release_date,
                        "commodity": commodity,
                        "crop_year": crop_year,
                        "role": role,
                        "vintage_month": vintage_month,
                        "unit": "million_metric_tons",
                        **values,
                        "data_available": not unavailable,
                        "stocks_to_domestic_use": None
                        if unavailable
                        else values["ending_stocks"] / values["domestic_use"],
                    }
                )
    keys = [(r["commodity"], r["crop_year"], r["role"]) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate world vintage")
    if not rows:
        raise ValueError("no world vintages")
    return rows


def revision_rows(panel: list[dict]) -> list[dict]:
    """Actual previous-report current is authoritative; restatement is only an audit."""
    currents = {
        (r["release_date"], r["commodity"], r["crop_year"]): r
        for r in panel
        if r["role"] == "current"
    }
    prior_rows = {
        (r["release_date"], r["commodity"], r["crop_year"]): r
        for r in panel
        if r["role"] == "prior_as_restated"
    }
    dates = sorted({r["release_date"] for r in panel})
    result = []
    for previous_date, current_date in zip(dates, dates[1:], strict=False):
        previous_month = date.fromisoformat(previous_date)
        current_month = date.fromisoformat(current_date)
        if (
            current_month.year - previous_month.year
        ) * 12 + current_month.month - previous_month.month != 1:
            continue
        for (release, commodity, year), current in currents.items():
            if release != current_date:
                continue
            previous = currents.get((previous_date, commodity, year))
            if previous is None:
                continue
            restated = prior_rows.get((current_date, commodity, year))
            prior_ratio = None if restated is None else restated["stocks_to_domestic_use"]
            ratio_revision = current["stocks_to_domestic_use"] - previous["stocks_to_domestic_use"]
            newest_crop = max(
                year_key
                for release_key, commodity_key, year_key in currents
                if release_key == release and commodity_key == commodity
            )
            result.append(
                {
                    "release_date": current_date,
                    "previous_release_date": previous_date,
                    "commodity": commodity,
                    "crop_year": year,
                    "is_latest_projected_crop_year": year == newest_crop,
                    "previous_report_current_ratio": previous["stocks_to_domestic_use"],
                    "current_report_current_ratio": current["stocks_to_domestic_use"],
                    "ratio_revision": ratio_revision,
                    "tightening_direction": 1
                    if ratio_revision < 0
                    else (-1 if ratio_revision > 0 else 0),
                    "restated_prior_ratio": prior_ratio,
                    "restatement_difference": None
                    if prior_ratio is None
                    else prior_ratio - previous["stocks_to_domestic_use"],
                    "historical_availability_certified": False,
                }
            )
    return result


def conservative_entry_date(release_date: str, trading_dates: list[str]) -> str | None:
    """Date-only modeling bound, NOT historical publication certification.

    A DCE daily label can open at 21:00 of the preceding trading date.
    Require even that earliest possible open to follow the full New York
    publication date. This also conservatively excludes uncertain night bars.
    """
    upper_bound = datetime.combine(
        date.fromisoformat(release_date), time(23, 59, 59), tzinfo=ZoneInfo("America/New_York")
    )
    return entry_date_after_archive(upper_bound.isoformat(), trading_dates)


def entry_date_after_archive(archive_available_at: str, trading_dates: list[str]) -> str | None:
    """Next complete DCE session under an explicit archived-time research assumption."""
    upper_bound = datetime.fromisoformat(archive_available_at.replace("Z", "+00:00"))
    if upper_bound.tzinfo is None:
        raise ValueError("naive archive availability")
    dates = sorted(set(trading_dates))
    for previous, current in zip(dates, dates[1:], strict=False):
        earliest = datetime.combine(
            date.fromisoformat(previous), time(21), tzinfo=ZoneInfo("Asia/Shanghai")
        )
        if earliest > upper_bound:
            return current
    return None


def derive_availability(entry: dict, metadata_text: str) -> dict:
    """Preserve both contradictory clocks and model availability as their maximum."""
    blocks = re.findall(r'field--name-release-date.*?<time datetime="([^"]+)"', metadata_text, re.S)
    if len(blocks) != 1:
        raise ValueError("missing or ambiguous release datetime")
    html_time = datetime.fromisoformat(blocks[0].replace("Z", "+00:00"))
    headers = {key.lower(): value for key, value in entry["headers"].items()}
    if not headers.get("last-modified"):
        raise ValueError("missing TXT last-modified")
    modified_time = parsedate_to_datetime(headers["last-modified"])
    captured_time = datetime.fromisoformat(entry["observed_at"])
    if any(value.tzinfo is None for value in [html_time, modified_time, captured_time]):
        raise ValueError("naive archived clock")
    if html_time.date().isoformat() != entry["report_date"]:
        raise ValueError("release date identity mismatch")
    if modified_time.date() < html_time.date() or max(html_time, modified_time) > captured_time:
        raise ValueError("archived clock contradicts release or capture date")
    return {
        "report_date": entry["report_date"],
        "source_sha256": entry["sha256"],
        "html_release_datetime": html_time.isoformat(),
        "txt_last_modified": modified_time.isoformat(),
        "archive_available_at": max(html_time, modified_time).isoformat(),
        "actual_captured_at": captured_time.isoformat(),
        "archive_time_assumption_eligible": True,
        "historical_availability_certified": False,
        "qualification": "maximum archived clocks is a research assumption; original immutable publication chain unproven",
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fields)
        writer.writeheader()
        writer.writerows(rows)


def capture(url: str, output: Path, identifier: str) -> dict:
    """Bounded public capture; failures and full response originals are retained."""
    if urlparse(url).hostname != "esmis.nal.usda.gov":
        raise ValueError("unexpected archive host")
    target = output / "originals" / (identifier + ".bin")
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": identifier,
        "url": url,
        "request_started_at": datetime.now(timezone.utc).isoformat(),
        "historical_availability_certified": False,
    }
    try:
        with urlopen(
            Request(url, headers={"User-Agent": "afuture-offline-research/1.0"}), timeout=15
        ) as response:
            body = response.read(4_000_001)
            if len(body) > 4_000_000:
                raise ValueError("response exceeds protocol cap")
            entry.update(
                http_status=response.status,
                response_url=response.url,
                headers=dict(response.headers),
            )
    except Exception as error:
        body = error.read(4_000_001) if hasattr(error, "read") else b""
        entry.update(error=repr(error), http_status=getattr(error, "code", None))
    target.write_bytes(body)
    entry.update(
        file=str(target.relative_to(output)),
        bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    (output / (identifier + ".json")).write_text(json.dumps(entry, indent=2) + "\n")
    return entry


def release_links(text: str) -> list[str]:
    return sorted(
        set(
            urljoin(ARCHIVE, url)
            for url in re.findall(
                r'href="([^"]*world-agricultural-supply-and-demand-estimates/20\d{2}-\d{2}-\d{2}[^"?]*)"',
                text,
            )
        )
    )


def build_from_manifest(manifest: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    panel, rejected = [], []
    for entry in json.loads(manifest.read_text()):
        if not entry.get("report_date"):
            continue
        source = Path(entry["local_path"])
        body = source.read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise ValueError("original identity mismatch")
        try:
            rows = parse_report(body.decode("utf-8-sig"), entry["report_date"])
        except ValueError as error:
            rejected.append({"path": str(source), "reason": str(error)})
            continue
        for row in rows:
            row.update(
                source_path=str(source),
                source_sha256=entry["sha256"],
                captured_at=entry["observed_at"],
                historical_availability_certified=False,
            )
        panel.extend(rows)
    revisions = revision_rows(panel)
    write_csv(output / "vintage_panel.csv", panel)
    write_csv(output / "revision_panel.csv", revisions)
    status = {
        "parsed_reports": len({r["release_date"] for r in panel}),
        "vintage_rows": len(panel),
        "revision_rows": len(revisions),
        "report_dates": sorted({r["release_date"] for r in panel}),
        "latest_crop_revision_rows": sum(r["is_latest_projected_crop_year"] for r in revisions),
        "explicit_unavailable_prior_rows": sum(not r["data_available"] for r in panel),
        "nonzero_restatements": sum(
            r["restatement_difference"] is not None and abs(r["restatement_difference"]) > 1e-12
            for r in revisions
        ),
        "rejected_reports": rejected,
        "historical_PIT_certified_reports": 0,
        "signal_semantics": "official forecast revision, not market consensus surprise",
        "qualification": "research-vintage panel only; release clock and immutable original chain remain uncertified",
    }
    (output / "data_status.json").write_text(json.dumps(status, indent=2) + "\n")
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_from_manifest(args.manifest, args.output), indent=2))


if __name__ == "__main__":
    main()
