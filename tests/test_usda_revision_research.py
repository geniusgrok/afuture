import pytest

from tools.usda_revision_research import (
    conservative_entry_date,
    derive_availability,
    entry_date_after_archive,
    parse_report,
    revision_rows,
)


def report(prior="10.00", current="9.00", year="2026/27"):
    sections = []
    for name, crush in [("Soybean", " 40.00"), ("Soybean Meal", ""), ("Soybean Oil", "")]:
        prior_values = (
            "NA " * (7 if crush else 6)
            if prior == "NA"
            else f"10.00 100.00 2.00{crush} 90.00 10.00 {prior}"
        )
        sections.append(
            f"World {name} Supply and Use\n(Million Metric Tons)\n{year} Proj.\nWorld  2/\n Aug {prior_values}\n Sep 10.00 100.00 2.00{crush} 90.00 10.00 {current}\nWASDE - 999\n"
        )
    return "".join(sections)


def test_current_and_restated_prior_are_distinct_and_units_checked():
    rows = parse_report(report(), "2026-09-11")
    assert len(rows) == 6
    soybean = [r for r in rows if r["commodity"] == "soybeans"]
    assert soybean[0]["ending_stocks"] == 10
    assert soybean[1]["ending_stocks"] == 9
    assert soybean[1]["domestic_use"] == 90
    assert soybean[1]["crush"] == 40
    with pytest.raises(ValueError, match="units"):
        parse_report(report().replace("Million Metric Tons", "bushels"), "2026-09-11")


def test_revision_uses_actual_previous_report_and_never_bridges_crop_years():
    previous = parse_report(
        report(current="8.00").replace("Aug", "Jul").replace("Sep", "Aug"), "2026-08-12"
    )
    current = parse_report(report(), "2026-09-11")
    revisions = revision_rows(previous + current)
    assert len(revisions) == 3
    assert revisions[0]["ratio_revision"] == pytest.approx(1 / 90)
    assert revisions[0]["restatement_difference"] == pytest.approx(2 / 90)
    other_year = parse_report(report(year="2027/28"), "2026-09-11")
    assert revision_rows(previous + other_year) == []


def test_unavailable_prior_is_preserved_without_zero_imputation():
    rows = parse_report(report(prior="NA"), "2026-09-11")
    prior = [r for r in rows if r["role"] == "prior_as_restated"]
    assert len(prior) == 3
    assert all(not r["data_available"] and r["ending_stocks"] is None for r in prior)
    assert all(r["stocks_to_domestic_use"] == 0.1 for r in rows if r["role"] == "current")


def test_night_session_and_weekend_do_not_admit_earlier_daily_open():
    assert (
        conservative_entry_date("2026-08-12", ["2026-08-12", "2026-08-13", "2026-08-14"])
        == "2026-08-14"
    )
    assert (
        conservative_entry_date("2026-09-11", ["2026-09-11", "2026-09-14", "2026-09-15"])
        == "2026-09-15"
    )
    assert conservative_entry_date("2026-09-11", ["2026-09-11", "2026-09-14"]) is None


def test_later_file_revision_delays_earliest_entry_and_capture_is_separate():
    entry = {
        "report_date": "2026-05-12",
        "sha256": "identity",
        "observed_at": "2026-10-04T09:00:00+00:00",
        "headers": {"last-modified": "Wed, 13 May 2026 14:55:26 GMT"},
    }
    html = 'field--name-release-date <time datetime="2026-05-12T12:00:00Z">'
    row = derive_availability(entry, html)
    assert row["archive_available_at"] == "2026-05-13T14:55:26+00:00"
    assert row["actual_captured_at"] == entry["observed_at"]
    assert row["historical_availability_certified"] is False
    assert (
        entry_date_after_archive(
            row["archive_available_at"], ["2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15"]
        )
        == "2026-05-15"
    )
    with pytest.raises(ValueError, match="missing TXT"):
        derive_availability({**entry, "headers": {}}, html)
    with pytest.raises(ValueError, match="identity mismatch"):
        derive_availability(entry, html.replace("2026-05-12", "2026-05-11"))


def test_missing_month_is_not_assumed_to_be_an_adjacent_report():
    earlier = parse_report(report(), "2026-09-11")
    later = parse_report(report().replace("Aug", "Oct").replace("Sep", "Nov"), "2026-11-12")
    assert revision_rows(earlier + later) == []
