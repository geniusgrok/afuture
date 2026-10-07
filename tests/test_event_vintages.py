import hashlib
from datetime import datetime, timezone

import pytest

from afuture.event_vintages import captured_version_available_at, version_usable_before_entry


def metadata(payload=b"report"):
    return dict(
        http_status=200,
        url="https://esmis.nal.usda.gov/report.txt",
        response_url="https://esmis.nal.usda.gov/report.txt",
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        request_started_at="2026-10-04T10:00:00+00:00",
        observed_at="2026-10-04T10:00:01+00:00",
        headers={"last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
    )


def test_availability_is_actual_capture_not_older_publication_or_production_clock():
    meta = metadata()
    assert captured_version_available_at(meta, b"report") == datetime(
        2026, 10, 4, 10, 0, 1, tzinfo=timezone.utc
    )
    assert not version_usable_before_entry(
        meta, b"report", datetime(2026, 9, 30, tzinfo=timezone.utc)
    )
    assert version_usable_before_entry(meta, b"report", datetime(2026, 10, 5, tzinfo=timezone.utc))


def test_revised_payload_and_naive_or_reversed_clocks_fail_closed():
    with pytest.raises(ValueError, match="version"):
        captured_version_available_at(metadata(), b"revised")
    for value in ("2026-10-04T10:00:01", "2026-10-04T09:00:00+00:00"):
        meta = metadata()
        meta["observed_at"] = value
        with pytest.raises(ValueError, match="clock"):
            captured_version_available_at(meta, b"report")
