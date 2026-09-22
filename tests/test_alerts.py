from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Event
from time import monotonic, time

import pytest


class _BlockingResponse:
    def __init__(self, entered: Event, release: Event) -> None:
        self.entered = entered
        self.release = release

    def __enter__(self):
        self.entered.set()
        self.release.wait(timeout=5)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    status = 200

    def geturl(self) -> str:
        return "https://example.invalid/hook"

    def read(self, size: int) -> bytes:
        del size
        return b""


def test_webhook_send_never_waits_for_network_and_spool_is_bounded(tmp_path, monkeypatch):
    import afuture.alerts as alerts

    entered = Event()
    release = Event()
    monkeypatch.setattr(
        alerts,
        "urlopen",
        lambda *_args, **_kwargs: _BlockingResponse(entered, release),
    )
    sink = alerts.WebhookAlertSink(
        "https://example.invalid/hook", spool_path=tmp_path / "outbox.db", max_queue=2
    )
    try:
        started = monotonic()
        sink.send({"message": "first"})
        assert monotonic() - started < 0.1
        assert entered.wait(timeout=1)

        sink.send({"message": "queued"})
        with pytest.raises(alerts.AlertDeliveryError, match="full"):
            sink.send({"message": "cannot silently drop"})
        assert sink.pending_count == 2
    finally:
        release.set()
        sink.close(timeout_seconds=1)


def test_local_alert_evidence_is_written_before_blocked_webhook_delivery(
    tmp_path: Path,
    monkeypatch,
):
    import afuture.alerts as alerts

    entered = Event()
    release = Event()
    monkeypatch.setattr(
        alerts,
        "urlopen",
        lambda *_args, **_kwargs: _BlockingResponse(entered, release),
    )
    path = tmp_path / "alerts.jsonl"
    webhook = alerts.WebhookAlertSink(
        "https://example.invalid/hook", spool_path=tmp_path / "outbox.db"
    )
    manager = alerts.AlertManager([alerts.FileAlertSink(path), webhook])
    try:
        manager.critical("halt now", {"reason": "unknown trade"})

        payload = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        assert payload["message"] == "halt now"
        assert payload["details"] == {"reason": "unknown trade"}
        assert entered.wait(timeout=1)
    finally:
        release.set()
        manager.close(timeout_seconds=1)


def test_alert_manager_close_is_bounded_and_retains_pending_while_webhook_is_stuck(
    tmp_path, monkeypatch
):
    import afuture.alerts as alerts

    entered = Event()
    release = Event()
    monkeypatch.setattr(
        alerts,
        "urlopen",
        lambda *_args, **_kwargs: _BlockingResponse(entered, release),
    )
    webhook = alerts.WebhookAlertSink(
        "https://example.invalid/hook", spool_path=tmp_path / "outbox.db"
    )
    manager = alerts.AlertManager([webhook])
    manager.critical("halt")
    assert entered.wait(timeout=1)
    manager.critical("queued-1")
    manager.critical("queued-2")

    started = monotonic()
    manager.close(timeout_seconds=0.01)
    assert monotonic() - started < 0.2
    assert webhook.pending_count == 3
    release.set()
    webhook.close(timeout_seconds=1)
    recovered = alerts.WebhookAlertSink(
        "https://example.invalid/hook",
        spool_path=tmp_path / "outbox.db",
        start_worker=False,
    )
    try:
        assert recovered.pending_count == 2
        assert recovered.deliver_one()
        assert recovered.deliver_one()
        assert recovered.pending_count == 0
    finally:
        recovered.close()


def test_failed_retry_budget_and_incident_dedup_survive_restart(tmp_path, monkeypatch):
    import afuture.alerts as alerts

    def failure(*_args):
        raise TimeoutError("ambiguous HTTP response")

    monkeypatch.setattr(alerts.WebhookAlertSink, "_deliver", failure)
    path = tmp_path / "outbox.db"
    sink = alerts.WebhookAlertSink(
        "https://example.invalid/hook",
        spool_path=path,
        start_worker=False,
        max_attempts=2,
    )
    sink.send({"timestamp": "first", "level": "CRITICAL", "message": "halt"})
    sink.send({"timestamp": "second", "level": "CRITICAL", "message": "halt"})
    assert sink.pending_count == 1
    assert sink.deliver_one()
    sink.close()
    sink = alerts.WebhookAlertSink(
        "https://example.invalid/hook",
        spool_path=path,
        start_worker=False,
        max_attempts=2,
    )
    assert sink.deliver_one(now=time() + 2)
    assert sink.diagnostics()["failed_count"] == 1
    sink.close()
    sink = alerts.WebhookAlertSink(
        "https://example.invalid/hook",
        spool_path=path,
        start_worker=False,
        max_attempts=2,
    )
    assert not sink.deliver_one(now=time() + 100)
    assert sink.diagnostics()["failed_count"] == 1
    assert sink.diagnostics()["human_read_verified"] is False
    sink.close()


def test_killed_delivery_replays_same_event_id_not_a_new_notification(tmp_path, monkeypatch):
    import sqlite3
    import subprocess
    import sys

    import afuture.alerts as alerts

    path = tmp_path / "outbox.db"
    code = """
import os, sys
from afuture.alerts import WebhookAlertSink
sink = WebhookAlertSink('https://example.invalid/hook', spool_path=sys.argv[1], start_worker=False)
sink.send({'message': 'critical before power loss'})
sink._deliver = lambda *_: os._exit(13)
sink.deliver_one()
"""
    result = subprocess.run([sys.executable, "-c", code, str(path)], timeout=10, check=False)
    assert result.returncode == 13
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT event_id,status,attempts FROM outbox").fetchone()
    assert original[1:] == ("inflight", 1)
    accepted = []
    monkeypatch.setattr(
        alerts.WebhookAlertSink,
        "_deliver",
        lambda _self, event_id, payload: accepted.append((event_id, payload)) or 202,
    )
    sink = alerts.WebhookAlertSink(
        "https://example.invalid/hook",
        spool_path=path,
        start_worker=False,
    )
    try:
        assert sink.deliver_one(now=time() + 10)
        assert accepted[0][0] == original[0]
        assert sink.pending_count == 0
        assert sink.diagnostics()["last_http_accepted_utc"] is not None
    finally:
        sink.close()


def test_spool_refuses_new_destination_and_corrupt_file_without_reset(tmp_path):
    import afuture.alerts as alerts

    path = tmp_path / "outbox.db"
    sink = alerts.WebhookAlertSink(
        "https://example.invalid/hook",
        spool_path=path,
        start_worker=False,
    )
    sink.send({"message": "pending for original destination"})
    sink.close()
    before = path.read_bytes()
    with pytest.raises(alerts.AlertDeliveryError, match="identity"):
        alerts.WebhookAlertSink(
            "https://other.invalid/hook",
            spool_path=path,
            start_worker=False,
        )
    assert path.read_bytes() == before
    path.write_bytes(b"corrupt")
    with pytest.raises(sqlite3.DatabaseError):
        alerts.WebhookAlertSink(
            "https://example.invalid/hook",
            spool_path=path,
            start_worker=False,
        )
    assert path.read_bytes() == b"corrupt"


def test_two_workers_do_not_claim_same_inflight_event(tmp_path, monkeypatch):
    import afuture.alerts as alerts

    entered, release = Event(), Event()
    monkeypatch.setattr(alerts, "urlopen", lambda *_a, **_k: _BlockingResponse(entered, release))
    first = alerts.WebhookAlertSink(
        "https://example.invalid/hook", spool_path=tmp_path / "outbox.db"
    )
    second = alerts.WebhookAlertSink(
        "https://example.invalid/hook",
        spool_path=tmp_path / "outbox.db",
        start_worker=False,
    )
    try:
        first.send({"message": "single incident"})
        assert entered.wait(timeout=1)
        assert not second.deliver_one()
        assert second.pending_count == 1
    finally:
        release.set()
        first.close()
        second.close()


def test_cli_alert_manager_uses_durable_spool_and_reports_failed_persistence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import afuture.alerts as alerts
    from afuture.cli import _build_alert_manager

    monkeypatch.setattr(alerts.WebhookAlertSink, "_deliver", lambda *_: 204)
    config = SimpleNamespace(
        alert_path=str(tmp_path / "alerts.jsonl"), alert_webhook="https://example.invalid/hook"
    )
    manager = _build_alert_manager(config)
    try:
        manager.critical("real production factory")
        assert (tmp_path / "alerts.outbox.sqlite3").exists()
        manager.critical("x" * 20_000)
        assert manager.diagnostics()["persistence_failures"] == 1
        assert manager.diagnostics()["deliveries"][0]["human_read_verified"] is False
    finally:
        manager.close()


def test_closed_spool_does_not_enter_closed_sqlite_connection(tmp_path):
    from afuture.alerts import AlertDeliveryError, WebhookAlertSink

    sink = WebhookAlertSink(
        "https://example.invalid/alert", spool_path=tmp_path / "closed.sqlite3", start_worker=False
    )
    sink.close()
    with pytest.raises(AlertDeliveryError, match="closed"):
        sink.send({"message": "cannot silently accept after close"})
    assert not sink.deliver_one()


def test_webhook_redirect_is_rejected_before_forwarding_payload():
    from urllib.request import Request

    from afuture.alerts import AlertDeliveryError, _RejectWebhookRedirects

    request = Request("https://example.invalid/hook", data=b"private notification", method="POST")
    with pytest.raises(AlertDeliveryError, match="redirects"):
        _RejectWebhookRedirects().redirect_request(
            request, None, 307, "redirect", {}, "https://other.invalid/collect"
        )
