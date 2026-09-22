from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from time import monotonic

import pytest


class _BlockingResponse:
    status = 200

    def __init__(self, entered: Event, release: Event) -> None:
        self.entered = entered
        self.release = release

    def __enter__(self):
        self.entered.set()
        self.release.wait(timeout=5)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def read(self, size: int) -> bytes:
        del size
        return b""


def test_webhook_send_never_waits_for_network_and_full_outbox_is_not_silent(tmp_path, monkeypatch):
    import afuture.alerts as alerts

    entered = Event()
    release = Event()
    monkeypatch.setattr(
        alerts,
        "urlopen",
        lambda *_args, **_kwargs: _BlockingResponse(entered, release),
    )
    sink = alerts.WebhookAlertSink(
        "https://example.invalid/hook", outbox_path=tmp_path / "outbox.db", max_pending=2
    )
    try:
        started = monotonic()
        sink.send({"message": "first"})
        assert monotonic() - started < 0.1
        assert entered.wait(timeout=1)

        sink.send({"message": "queued"})
        with pytest.raises(RuntimeError, match="durability"):
            sink.send({"message": "cannot-fit"})
        assert sink.pending_count == 2
        assert sink.delivery_status()["error"]
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
        "https://example.invalid/hook", outbox_path=tmp_path / "outbox.db"
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


def test_alert_manager_close_is_bounded_and_retains_undelivered_events(tmp_path, monkeypatch):
    import afuture.alerts as alerts

    entered = Event()
    release = Event()
    monkeypatch.setattr(
        alerts,
        "urlopen",
        lambda *_args, **_kwargs: _BlockingResponse(entered, release),
    )
    webhook = alerts.WebhookAlertSink(
        "https://example.invalid/hook", outbox_path=tmp_path / "outbox.db"
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
    assert webhook.delivery_status()["http_accepted"] == 0
    release.set()


def test_outbox_restart_keeps_identity_attempts_and_http_receipt(tmp_path):
    from afuture.alert_outbox import AlertOutbox, AlertOutboxError

    path = tmp_path / "outbox.db"
    first = AlertOutbox(path, "https://example.invalid/a")
    event = {"timestamp": "2026-09-23T01:00:00Z", "message": "unknown order", "level": "CRITICAL"}
    identity = first.enqueue(event)
    assert first.enqueue(event) == identity
    restarted = AlertOutbox(path, "https://example.invalid/a")
    selected = restarted.next_event()
    assert selected == (identity, event, 1)
    assert restarted.status()["pending"] == 1
    with pytest.raises(AlertOutboxError, match="endpoint changed"):
        AlertOutbox(path, "https://example.invalid/b")
    with pytest.raises(AlertOutboxError, match="did not accept"):
        restarted.accepted(identity, 500)
    restarted.accepted(identity, 202)
    status = AlertOutbox(path, "https://example.invalid/a").status()
    assert status["pending"] == 0
    assert status["http_accepted"] == 1
    assert status["delivery_evidence"] == "http_acceptance_not_human_read"
