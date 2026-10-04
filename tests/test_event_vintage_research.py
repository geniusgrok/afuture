import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import event_vintage_research as research


def fake_capture(monkeypatch, output, pages):
    calls = []

    def capture(url, folder, identifier):
        assert folder == output
        calls.append(url)
        payload = pages[url].encode()
        target = folder / (identifier + ".bin")
        target.write_bytes(payload)
        return {
            "id": identifier,
            "file": target.name,
            "http_status": 200,
            "url": url,
            "response_url": url,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "request_started_at": "2026-10-04T16:00:00+00:00",
            "observed_at": "2026-10-04T16:00:01+00:00",
        }

    monkeypatch.setattr(research, "capture", capture)
    return calls


def test_current_discovery_is_bounded_and_does_not_backdate_versions(monkeypatch, tmp_path):
    base = "https://esmis.nal.usda.gov"
    releases = [
        f"{base}/world-agricultural-supply-and-demand-estimates/{day}"
        for day in ("2026-07-10", "2026-08-12", "2026-09-11")
    ]
    pages = {research.ARCHIVE: "".join(f'<a href="{url}">release</a>' for url in releases)}
    for url in releases:
        pages[url] = '<a href="/report.txt">text</a>'
    pages[base + "/report.txt"] = "official bytes"
    calls = fake_capture(monkeypatch, tmp_path, pages)
    versions = research.capture_latest_releases(tmp_path)
    assert len(calls) == 5 and releases[0] not in calls
    assert [row["report_date"] for row in versions] == ["2026-08-12", "2026-09-11"]
    for row in versions:
        available = research.captured_version_available_at(row, b"official bytes")
        assert available.isoformat() == "2026-10-04T16:00:01+00:00"


def test_ambiguous_official_report_links_are_preserved_but_rejected(monkeypatch, tmp_path):
    url = "https://esmis.nal.usda.gov/world-agricultural-supply-and-demand-estimates/2026-09-11"
    pages = {
        research.ARCHIVE: f'<a href="{url}">release</a>',
        url: '<a href="/first.txt">one</a><a href="/second.txt">two</a>',
    }
    calls = fake_capture(monkeypatch, tmp_path, pages)
    with pytest.raises(ValueError, match="ambiguous"):
        research.capture_latest_releases(tmp_path)
    assert len(calls) == 2 and len(list(tmp_path.glob("*.bin"))) == 2
