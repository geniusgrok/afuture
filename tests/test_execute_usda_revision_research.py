"""Vintages, missing publications and late arrivals cannot become future signals."""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from execute_usda_revision_research import next_experiment, qualify_result, revision_targets


def available(date, timestamp=None):
    return {
        "report_date": date,
        "archive_available_at": timestamp or date + "T16:01:00+00:00",
        "archive_time_assumption_eligible": True,
    }


def revision(date, prior, crop, change, latest=False):
    return {
        "release_date": date,
        "previous_release_date": prior,
        "commodity": "soybeans",
        "crop_year": crop,
        "is_latest_projected_crop_year": latest,
        "previous_report_current_ratio": 0.3,
        "current_report_current_ratio": 0.3 + change,
        "ratio_revision": change,
    }


def test_latest_common_crop_survives_new_crop_first_publication_and_waits_for_entry():
    dates = pd.bdate_range("2025-04-01", "2025-06-20")
    panel = pd.DataFrame(
        [
            revision("2025-05-12", "2025-04-10", "2023/24", 0.01),
            revision("2025-05-12", "2025-04-10", "2024/25", -0.02),
        ]
    )
    weights, audit = revision_targets(
        panel, [available("2025-04-10"), available("2025-05-12")], dates
    )
    assert weights.loc[:"2025-05-13"].eq(0).all().all()
    assert weights.loc["2025-05-14", "B"] == 2 / 3
    assert audit.loc[audit.reason.eq("latest_common_crop_year"), "crop_year"].iloc[0] == "2024/25"
    assert weights.AG.eq(0).all() and weights.M.eq(0).all()


def test_report_without_adjacent_month_revision_clears_previous_signal():
    dates = pd.bdate_range("2025-08-01", "2025-11-25")
    panel = pd.DataFrame([revision("2025-09-12", "2025-08-12", "2025/26", -0.01)])
    weights, _ = revision_targets(
        panel, [available("2025-08-12"), available("2025-09-12"), available("2025-11-14")], dates
    )
    assert weights.loc["2025-11-14", "B"] == 2 / 3
    assert weights.loc["2025-11-18":, "B"].eq(0).all()


def test_late_older_vintage_cannot_overwrite_newer_known_report():
    dates = pd.bdate_range("2025-04-01", "2025-07-01")
    panel = pd.DataFrame(
        [
            revision("2025-05-12", "2025-04-10", "2024/25", 0.01),
            revision("2025-06-12", "2025-05-12", "2024/25", -0.01),
        ]
    )
    availability = [
        available("2025-04-10"),
        available("2025-05-12"),
        available("2025-06-12"),
        available("2025-03-11", "2025-06-20T16:00:00+00:00"),
    ]
    weights, audit = revision_targets(panel, availability, dates)
    assert weights.loc["2025-06-16":, "B"].eq(2 / 3).all()
    assert audit.reason.eq("older_late_report_ignored").any()


def test_archive_assumption_never_certifies_publication_or_live_even_if_core_passes():
    result = qualify_result({"economic_passed": True})
    assert result["archive_time_assumption"]
    assert not result["historical_availability_certified"] and not result["qualified_for_live"]
    assert (
        result["acceptance_status"] == "pending_publication_timing_and_original_extended_acceptance"
    )


def test_real_2025_late_archive_batch_activates_may_without_reverting_to_february():
    dates = pd.bdate_range("2025-01-01", "2025-06-10")
    panel = pd.DataFrame(
        [
            revision("2025-02-11", "2025-01-10", "2024/25", -0.01),
            revision("2025-03-11", "2025-02-11", "2024/25", -0.01),
            revision("2025-04-10", "2025-03-11", "2024/25", -0.01),
            revision("2025-05-12", "2025-04-10", "2024/25", 0.01),
        ]
    )
    manifests = [
        available("2025-01-10"),
        available("2025-02-11", "2025-06-03T20:37:50+00:00"),
        available("2025-03-11", "2025-06-03T20:37:48+00:00"),
        available("2025-04-10", "2025-06-03T20:37:45+00:00"),
        available("2025-05-12", "2025-06-03T20:37:42+00:00"),
    ]
    weights, audit = revision_targets(panel, manifests, dates)
    assert weights.loc[:"2025-06-04", "B"].eq(0).all()
    assert weights.loc["2025-06-05":, "B"].eq(-2 / 3).all()
    assert audit.reason.eq("older_late_signal_ignored").sum() == 3
    assert audit.loc[audit.reason.eq("latest_common_crop_year"), "release_date"].tolist() == [
        "2025-05-12"
    ]


def test_report_waits_for_delayed_prior_vintage_then_activates_current_revision():
    dates = pd.bdate_range("2025-04-01", "2025-06-10")
    panel = pd.DataFrame([revision("2025-05-12", "2025-04-10", "2024/25", -0.01)])
    availability = [available("2025-04-10", "2025-05-20T16:00:00+00:00"), available("2025-05-12")]
    weights, _ = revision_targets(panel, availability, dates)
    assert weights.loc[:"2025-05-21", "B"].eq(0).all()
    assert weights.loc["2025-05-22":, "B"].eq(2 / 3).all()


def test_valid_core_failure_with_component_value_automatically_tests_shared_account():
    assert next_experiment([])["id"] == "U1"
    parent = {
        "attempt": "000001-u1",
        "spec": {"id": "U1"},
        "status": "economic_failed",
        "result": {
            "all_eight_account_ledgers_passed": True,
            "component_net_positive": True,
            "component_protection_observed": False,
        },
    }
    assert next_experiment([parent])["id"] == "U1MIX"
    parent["result"]["component_net_positive"] = False
    assert next_experiment([parent]) is None
    parent["result"]["component_protection_observed"] = True
    assert next_experiment([parent])["parent_attempt"] == "000001-u1"
    parent["status"] = "invalid_evidence"
    assert next_experiment([parent]) is None
    parent["status"] = "candidate_passed"
    assert next_experiment([parent]) is None
