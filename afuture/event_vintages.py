"""Byte-version availability from preserved public captures, not document dates."""

from __future__ import annotations

import hashlib
from datetime import datetime
from urllib.parse import urlparse


def captured_version_available_at(metadata: dict, payload: bytes) -> datetime:
    """Accept these exact public bytes no earlier than the completed capture.

    This does not certify their original publication time. Later revised bytes get
    a different identity and their own capture time; Last-Modified, PDF production
    time and report dates cannot move availability backwards.
    """
    if metadata.get("http_status") != 200 or metadata.get("error"):
        raise ValueError("successful original public capture required")
    for field in ("url", "response_url"):
        parsed = urlparse(metadata[field])
        if parsed.scheme != "https" or parsed.hostname != "esmis.nal.usda.gov":
            raise ValueError("unexpected public event source")
    if (
        len(payload) != metadata["bytes"]
        or hashlib.sha256(payload).hexdigest() != metadata["sha256"]
    ):
        raise ValueError("captured public byte version differs")
    start = datetime.fromisoformat(metadata["request_started_at"].replace("Z", "+00:00"))
    completed = datetime.fromisoformat(metadata["observed_at"].replace("Z", "+00:00"))
    if start.tzinfo is None or completed.tzinfo is None or start > completed:
        raise ValueError("public capture clock must be aware and ordered")
    return completed


def version_usable_before_entry(metadata: dict, payload: bytes, entry_at: datetime) -> bool:
    if entry_at.tzinfo is None:
        raise ValueError("entry clock must be timezone aware")
    return captured_version_available_at(metadata, payload) < entry_at
