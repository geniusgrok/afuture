"""Collect immutable SHFE supply observations and export only observed versions.

Research input only. Report dates, update_date and Last-Modified never backdate
availability. No strategy, account, order or production configuration is changed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8))
PRODUCT_NAMES = {"AL": "铝", "CU": "铜", "NI": "镍", "PB": "铅", "SN": "锡", "ZN": "锌"}
KINDS = ("weeklystock", "dailystock")
MAX_BYTES = 2_000_000
CALENDAR_URL = "https://www.shfe.com.cn/data/config/js/trade-data.js"


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("observation/decision timestamps must include timezone")
    return result.astimezone(UTC)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def store_raw(root: Path, raw: bytes) -> str:
    sha = digest(raw)
    path = root / "raw" / f"{sha}.dat"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError("immutable original does not match its content hash") from None
    return sha


def weekly_dates(raw: bytes, start: date, end: date) -> list[str]:
    """Use the official data calendar; group full weeks before applying bounds."""
    matches = re.findall(r"(20\d{2}):'([^']*)'", raw.decode("utf-8-sig"))
    excluded: dict[int, set[str]] = {}
    for year, values in matches:
        number = int(year)
        if number in excluded:
            raise ValueError("duplicate calendar year")
        excluded[number] = set(values.split(","))
    weeks: dict[tuple[int, int], date] = {}
    for year in range(start.year, end.year + 1):
        if year not in excluded:
            raise ValueError(f"calendar has no coverage for {year}")
        day = date(year, 1, 1)
        while day.year == year:
            if day.strftime("%Y%m%d") not in excluded[year]:
                if day.weekday() > 4:
                    raise ValueError("official data calendar includes a weekend")
                key = day.isocalendar()[:2]
                weeks[key] = day
            day += timedelta(days=1)
    return sorted(day.isoformat() for day in weeks.values() if start <= day <= end)


def amount(value: object) -> str:
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise ValueError("missing or boolean supply quantity")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid supply quantity") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("nonfinite or negative supply quantity")
    return str(number)


def parse_report(raw: bytes, kind: str, day: str) -> tuple[dict, list[dict]]:
    if kind not in KINDS:
        raise ValueError("unsupported supply report kind")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("o_cursor"), list):
        raise ValueError("official report has no stock table")
    if payload.get("o_code") not in ("0", "0000"):
        raise ValueError("official stock query was not successful")
    for field in ("report_date", "o_tradingday"):
        if str(payload.get(field, "")).replace("-", "") != day.replace("-", ""):
            raise ValueError(f"report date mismatch: {field}")
    rows: list[dict] = []
    seen: set[str] = set()
    for row in payload["o_cursor"]:
        if not isinstance(row, dict):
            raise ValueError("invalid stock row")
        product = str(row.get("VARID", "")).upper()
        name = str(row.get("VARNAME", "")).split("$$")[0]
        if not product:
            product = next((key for key, label in PRODUCT_NAMES.items() if label == name), "")
        if product not in PRODUCT_NAMES:
            continue
        if name != PRODUCT_NAMES[product]:
            raise ValueError("product code/name or warehouse scope mismatch")
        if str(row.get("ROWSTATUS")) != "2":
            continue
        if str(row.get("WHABBRNAME", "")).split("$$")[0] != "总计":
            continue  # bonded/tax subtotals are already included in the grand total
        legacy_daily = kind == "dailystock" and "WHTYPE" not in row and "VARID" not in row
        if not legacy_daily and str(row.get("WHTYPE")) != "1":
            raise ValueError("base-metal total is not a warehouse total")
        if product in seen:
            raise ValueError("duplicate product grand total")
        seen.add(product)
        if str(row.get("WGHTUNIT")) != "2":
            raise ValueError("unqualified base-metal quantity unit")
        result = {
            "product": product,
            "report_date": day,
            "kind": kind,
            "unit_code": "2",
            "warrants": amount(row.get("WRTWGHTS")),
        }
        if kind == "weeklystock":
            result["inventory_subtotal"] = amount(row.get("SPOTWGHTS"))
            result["available_capacity"] = amount(row.get("WHSTOCKS"))
        rows.append(result)
    if not rows:
        raise ValueError("report has no qualified base-metal grand totals")
    return payload, rows


def download(url: str) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": "afuture-personal-research/1.0"})
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read(MAX_BYTES + 1)
        metadata = {key: response.headers.get(key, "") for key in ("Date", "Last-Modified", "ETag")}
    if len(raw) > MAX_BYTES:
        raise ValueError("official response exceeds research bound")
    return raw, metadata


def capture(root: Path, kind: str, day: str) -> dict:
    directory = "weeklydata" if kind == "weeklystock" else "dailydata"
    compact = day.replace("-", "")
    url = f"https://www.shfe.com.cn/data/tradedata/future/{directory}/{compact}{kind}.dat"
    record = {"kind": kind, "report_date": day, "source_url": url, "started_at": utc_now()}
    try:
        raw, headers = download(url)
        observed_at = utc_now()
        record.update(
            observed_at=observed_at, bytes=len(raw), sha256=store_raw(root, raw), **headers
        )
        payload, rows = parse_report(raw, kind, day)
        if date.fromisoformat(day) > timestamp(observed_at).astimezone(SHANGHAI).date():
            raise ValueError("report is dated after the observation")
        record.update(
            status="ok",
            update_date=payload.get("update_date"),
            products=sorted(row["product"] for row in rows),
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
        record.update(status="error", error=f"{type(exc).__name__}: {exc}")
    return record


def records(root: Path) -> list[dict]:
    result: list[dict] = []
    for path in sorted((root / "observations").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            result.append(json.loads(line))  # truncated/corrupt receipts fail closed
    return result


def read_original(root: Path, record: dict) -> bytes:
    sha = record["sha256"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("invalid original identity")
    raw = (root / "raw" / f"{sha}.dat").read_bytes()
    if len(raw) != record["bytes"] or digest(raw) != sha:
        raise ValueError("original hash/length mismatch")
    return raw


def as_of(root: Path, decision_at: str) -> list[dict]:
    cutoff = timestamp(decision_at)
    latest: dict[tuple[str, str], dict] = {}
    first_seen: dict[tuple[str, str, str], str] = {}
    for record in records(root):
        if record["status"] != "ok" or timestamp(record["observed_at"]) > cutoff:
            continue
        if date.fromisoformat(record["report_date"]) > cutoff.astimezone(SHANGHAI).date():
            raise ValueError("future report date in observation receipt")
        key = record["kind"], record["report_date"]
        version = (*key, record["sha256"])
        initial = first_seen.get(version)
        if initial is None or timestamp(record["observed_at"]) < timestamp(initial):
            first_seen[version] = record["observed_at"]
        previous = latest.get(key)
        if previous is None or timestamp(record["observed_at"]) > timestamp(
            previous["observed_at"]
        ):
            latest[key] = record
        elif (
            timestamp(record["observed_at"]) == timestamp(previous["observed_at"])
            and record["sha256"] != previous["sha256"]
        ):
            raise ValueError("conflicting versions at the same observation time")
    result: list[dict] = []
    for key, record in sorted(latest.items()):
        _, rows = parse_report(read_original(root, record), *key)
        for row in rows:
            result.append(
                {
                    **row,
                    "available_at": record["observed_at"],
                    "first_observed_at": first_seen[(*key, record["sha256"])],
                    "sha256": record["sha256"],
                }
            )
    return result


def collect(scope: dict, root: Path, workers: int, refresh: bool) -> None:
    dates = scope["report_dates"]
    if sorted(set(dates)) != dates:
        raise ValueError("scope dates must be unique and sorted")
    if scope["products"] != sorted(PRODUCT_NAMES) or scope["kinds"] != list(KINDS):
        raise ValueError("unexpected research cohort or report kinds")
    for value in dates:
        date.fromisoformat(value)
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    calendar = (root / "trade-data.js").read_bytes()
    if digest(calendar) != scope["calendar_sha256"]:
        raise ValueError("scope calendar hash mismatch")
    expected = weekly_dates(
        calendar,
        date.fromisoformat(scope["collection_window"][0]),
        date.fromisoformat(scope["collection_window"][1]),
    )
    if dates != expected:
        raise ValueError("scope omits/adds dates outside the frozen calendar selection")
    old = records(root)
    for record in old:
        if record["status"] == "ok":
            parse_report(read_original(root, record), record["kind"], record["report_date"])
    done = {(r["kind"], r["report_date"]) for r in old if r["status"] == "ok"}
    tasks = [(kind, day) for day in dates for kind in KINDS if refresh or (kind, day) not in done]
    target = root / "observations"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex}.jsonl"
    with (
        path.open("x", encoding="utf-8") as ledger,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        futures = [pool.submit(capture, root, kind, day) for kind, day in tasks]
        ok = 0
        for count, future in enumerate(as_completed(futures), 1):
            record = future.result()
            ledger.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            ledger.flush()
            ok += record["status"] == "ok"
            if count % 10 == 0 or count == len(tasks):
                print(
                    f"observations={count}/{len(tasks)} usable={ok} failures={count - ok}",
                    flush=True,
                )


def revalidate(root: Path) -> None:
    """Reparse preserved bytes after a schema fix, retaining observation times."""
    recovered = []
    old = records(root)
    valid = {(r["sha256"], r["observed_at"]) for r in old if r["status"] == "ok"}
    for record in old:
        if record["status"] != "error" or "sha256" not in record:
            continue
        if (record["sha256"], record["observed_at"]) in valid:
            continue
        raw = read_original(root, record)
        try:
            payload, rows = parse_report(raw, record["kind"], record["report_date"])
        except (ValueError, TypeError):
            continue
        if (
            date.fromisoformat(record["report_date"])
            > timestamp(record["observed_at"]).astimezone(SHANGHAI).date()
        ):
            continue
        recovered.append(
            {
                **record,
                "status": "ok",
                "revalidated_at": utc_now(),
                "update_date": payload.get("update_date"),
                "products": sorted(row["product"] for row in rows),
            }
        )
        valid.add((record["sha256"], record["observed_at"]))
    path = root / "observations" / f"revalidated-{uuid4().hex}.jsonl"
    with path.open("x", encoding="utf-8") as handle:
        for row in recovered:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"revalidated_originals": len(recovered)}))


def qualify(scope: dict, root: Path) -> dict:
    all_records = records(root)
    by_key: dict[tuple[str, str], set[str]] = {}
    for record in all_records:
        if "sha256" in record:
            read_original(root, record)
        if record["status"] == "ok":
            _, rows = parse_report(
                read_original(root, record), record["kind"], record["report_date"]
            )
            by_key[record["kind"], record["report_date"]] = {row["product"] for row in rows}
    missing = [
        {
            "kind": kind,
            "report_date": day,
            "products": sorted(set(scope["products"]) - by_key.get((kind, day), set())),
        }
        for day in scope["report_dates"]
        for kind in scope["kinds"]
        if set(scope["products"]) - by_key.get((kind, day), set())
    ]
    # This bound assesses historical eligibility only; an actual replay must supply
    # its exact decision/session timestamps to as_of(), never just report dates.
    cutoff = scope["economic_window"][1] + "T23:59:59+00:00"
    historical = as_of(root, cutoff)
    historical_keys = {(row["kind"], row["report_date"], row["product"]) for row in historical}
    required_keys = {
        (kind, day, product)
        for day in scope["report_dates"]
        if day <= scope["economic_window"][1]
        for kind in scope["kinds"]
        for product in scope["products"]
    }
    status = "BLOCKED_NO_CONTEMPORANEOUS_VERSIONS"
    if historical_keys:
        status = "BLOCKED_INCOMPLETE_HISTORICAL_VERSIONS"
        if required_keys <= historical_keys:
            status = "END_WINDOW_INPUTS_AVAILABLE_EXACT_DECISION_TIMES_STILL_REQUIRED"
    versions: dict[tuple[str, str], set[str]] = {}
    for record in all_records:
        if "sha256" in record:
            versions.setdefault((record["kind"], record["report_date"]), set()).add(
                record["sha256"]
            )
    return {
        "schema_version": 1,
        "scope_sha256": digest(json.dumps(scope, sort_keys=True).encode()),
        "expected_reports": len(scope["report_dates"]) * len(scope["kinds"]),
        "usable_reports": len(by_key),
        "missing_product_reports": missing,
        "observations": len(all_records),
        "changed_reports": sum(len(v) > 1 for v in versions.values()),
        "historically_available_rows": len(historical),
        "economic_candidate_count": 0,
        "historical_account_status": status,
        "availability_policy": "exact bytes first observed; metadata never backdates availability",
        "quantity_policy": "inventory=SPOTWGHTS; warrants=WRTWGHTS; WHSTOCKS=capacity",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "revalidate", "qualify", "export"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--refresh", action="store_true", help="append observations, including revisions"
    )
    parser.add_argument("--decision-at", help="explicit timezone-aware research decision timestamp")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    scope = json.loads((args.root / "DATA_SCOPE.json").read_text(encoding="utf-8"))
    if args.command == "collect":
        collect(scope, args.root, args.workers, args.refresh)
    elif args.command == "revalidate":
        revalidate(args.root)
    elif args.command == "qualify":
        report = qualify(scope, args.root)
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        (args.output or args.root / "qualification.json").write_text(text, encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "missing_product_reports"}))
    else:
        if not args.decision_at or not args.output:
            parser.error("export requires --decision-at and --output")
        rows = as_of(args.root, args.decision_at)
        fields = [
            "product",
            "report_date",
            "kind",
            "unit_code",
            "inventory_subtotal",
            "available_capacity",
            "warrants",
            "available_at",
            "first_observed_at",
            "sha256",
        ]
        with args.output.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        print(json.dumps({"eligible_rows": len(rows), "decision_at": args.decision_at}))


if __name__ == "__main__":
    main()
