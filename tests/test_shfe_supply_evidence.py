from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from tools import shfe_supply_evidence as supply


def report(*, inventory=100, day="20250926", extra=()):
    row = {
        "VARID": "cu",
        "VARNAME": "铜$$COPPER",
        "ROWSTATUS": "2",
        "WHABBRNAME": "总计$$Total",
        "WHTYPE": "1",
        "WGHTUNIT": "2",
        "WHSTOCKS": 1000,
        "SPOTWGHTS": inventory,
        "WRTWGHTS": 25,
    }
    subtotal = {**row, "WHABBRNAME": "完税商品总计$$Total (Tax included)"}
    return json.dumps(
        {
            "o_code": "0000",
            "report_date": day,
            "o_tradingday": day,
            "update_date": "20250929 17:13:01",
            "o_cursor": [subtotal, row, *extra],
        },
        ensure_ascii=False,
    ).encode()


def append_observation(root, raw, observed_at, *, status="ok"):
    sha = supply.store_raw(root, raw)
    folder = root / "observations"
    folder.mkdir(exist_ok=True)
    record = {
        "status": status,
        "kind": "weeklystock",
        "report_date": "2025-09-26",
        "observed_at": observed_at,
        "sha256": sha,
        "bytes": len(raw),
    }
    with (folder / "receipt.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return sha


def test_inventory_capacity_and_warrants_do_not_mix_or_double_count():
    _, rows = supply.parse_report(report(), "weeklystock", "2025-09-26")
    assert len(rows) == 1
    assert rows[0]["inventory_subtotal"] == "100"
    assert rows[0]["available_capacity"] == "1000"
    assert rows[0]["warrants"] == "25"
    _, daily = supply.parse_report(report(), "dailystock", "2025-09-26")
    assert "inventory_subtotal" not in daily[0]


def test_legacy_daily_fields_and_report_date_validation():
    payload = json.loads(report())
    payload["o_code"] = "0"
    for row in payload["o_cursor"]:
        del row["VARID"]
        del row["WHTYPE"]
    assert supply.parse_report(json.dumps(payload).encode(), "dailystock", "2025-09-26")[1]
    with pytest.raises(ValueError, match="date mismatch"):
        supply.parse_report(report(), "weeklystock", "2025-09-25")
    with pytest.raises(ValueError, match="warehouse"):
        supply.parse_report(json.dumps(payload).encode(), "weeklystock", "2025-09-26")


@pytest.mark.parametrize(
    "field,value",
    [
        ("SPOTWGHTS", ""),
        ("SPOTWGHTS", -1),
        ("WRTWGHTS", "NaN"),
        ("SPOTWGHTS", True),
        ("WGHTUNIT", "1"),
        ("VARNAME", "铝$$ALUMINIUM"),
    ],
)
def test_unqualified_quantity_unit_or_product_fails_closed(field, value):
    payload = json.loads(report())
    payload["o_cursor"][1][field] = value
    with pytest.raises(ValueError):
        supply.parse_report(json.dumps(payload).encode(), "weeklystock", "2025-09-26")


def test_duplicate_total_is_rejected():
    payload = json.loads(report())
    with pytest.raises(ValueError, match="duplicate"):
        supply.parse_report(report(extra=[payload["o_cursor"][1]]), "weeklystock", "2025-09-26")


def test_late_publication_and_revision_do_not_rewrite_history(tmp_path):
    append_observation(tmp_path, report(), "2025-09-29T09:13:01+00:00")
    assert supply.as_of(tmp_path, "2025-09-26T21:00:00+08:00") == []
    append_observation(tmp_path, report(inventory=80), "2025-10-01T09:00:00+00:00")
    old = supply.as_of(tmp_path, "2025-09-30T12:00:00+00:00")
    new = supply.as_of(tmp_path, "2025-10-02T12:00:00+00:00")
    assert old[0]["inventory_subtotal"] == "100"
    assert new[0]["inventory_subtotal"] == "80"
    assert old[0]["sha256"] != new[0]["sha256"]


def test_repeated_observation_retains_first_version_time(tmp_path):
    append_observation(tmp_path, report(), "2025-09-29T09:00:00+00:00")
    append_observation(tmp_path, report(), "2025-09-30T09:00:00+00:00")
    row = supply.as_of(tmp_path, "2025-10-01T09:00:00+00:00")[0]
    assert row["first_observed_at"] == "2025-09-29T09:00:00+00:00"
    assert row["available_at"] == "2025-09-30T09:00:00+00:00"


def test_conflicting_versions_at_same_instant_with_different_timezones(tmp_path):
    append_observation(tmp_path, report(), "2025-09-29T09:00:00+00:00")
    append_observation(tmp_path, report(inventory=80), "2025-09-29T17:00:00+08:00")
    with pytest.raises(ValueError, match="conflicting versions"):
        supply.as_of(tmp_path, "2025-10-01T09:00:00+00:00")


def test_original_tampering_and_naive_decision_are_rejected(tmp_path):
    sha = append_observation(tmp_path, report(), "2025-09-29T09:00:00+00:00")
    (tmp_path / "raw" / f"{sha}.dat").write_bytes(b"{}")
    with pytest.raises(ValueError, match="hash/length"):
        supply.as_of(tmp_path, "2025-10-01T09:00:00+00:00")
    with pytest.raises(ValueError, match="timezone"):
        supply.as_of(tmp_path, "2025-10-01T09:00:00")


def test_calendar_uses_holiday_week_end_and_does_not_invent_partial_week():
    start = date(2025, 1, 1)
    blocked = []
    current = start
    while current.year == 2025:
        if current.weekday() > 4 or current == date(2025, 1, 3):
            blocked.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    raw = ("define(function(){return {2025:'" + ",".join(blocked) + "'};});").encode()
    assert supply.weekly_dates(raw, start, date(2025, 1, 8)) == ["2025-01-02"]


def test_failed_schema_keeps_original_and_is_not_eligible(tmp_path, monkeypatch):
    monkeypatch.setattr(supply, "download", lambda url: (b'{"unexpected":1}', {}))
    receipt = supply.capture(tmp_path, "weeklystock", "2025-09-26")
    assert receipt["status"] == "error"
    assert supply.read_original(tmp_path, receipt) == b'{"unexpected":1}'


def test_revalidation_keeps_real_observation_time_and_old_failure(tmp_path):
    append_observation(tmp_path, report(), "2026-10-02T03:00:00+00:00", status="error")
    supply.revalidate(tmp_path)
    receipts = supply.records(tmp_path)
    assert len(receipts) == 2
    assert {r["status"] for r in receipts} == {"error", "ok"}
    assert {r["observed_at"] for r in receipts} == {"2026-10-02T03:00:00+00:00"}
    assert supply.as_of(tmp_path, "2026-09-22T23:59:59+00:00") == []


def test_future_observation_cannot_qualify_historical_account(tmp_path):
    append_observation(tmp_path, report(), "2026-10-02T03:00:00+00:00")
    scope = {
        "report_dates": ["2025-09-26"],
        "products": sorted(supply.PRODUCT_NAMES),
        "kinds": list(supply.KINDS),
        "economic_window": ["2022-09-06", "2026-09-22"],
    }
    result = supply.qualify(scope, tmp_path)
    assert result["historically_available_rows"] == 0
    assert result["economic_candidate_count"] == 0
    assert result["historical_account_status"] == "BLOCKED_NO_CONTEMPORANEOUS_VERSIONS"
    assert len(result["missing_product_reports"]) == 2
