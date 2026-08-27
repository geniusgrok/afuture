from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from time import monotonic


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

    def read(self, size: int) -> bytes:
        del size
        return b""


def test_webhook_send_never_waits_for_network_and_queue_is_bounded(monkeypatch):
    import afuture.alerts as alerts

    entered = Event()
    release = Event()
    monkeypatch.setattr(
        alerts,
        "urlopen",
        lambda *_args, **_kwargs: _BlockingResponse(entered, release),
    )
    sink = alerts.WebhookAlertSink("https://example.invalid/hook", max_queue=2)
    try:
        started = monotonic()
        sink.send({"message": "first"})
        assert monotonic() - started < 0.1
        assert entered.wait(timeout=1)

        for index in range(10):
            sink.send({"message": f"queued-{index}"})

        assert sink.pending_count <= 2
        assert sink.dropped_count >= 1
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
    webhook = alerts.WebhookAlertSink("https://example.invalid/hook")
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


def test_alert_manager_close_is_bounded_while_webhook_is_stuck(monkeypatch):
    import afuture.alerts as alerts

    entered = Event()
    release = Event()
    monkeypatch.setattr(
        alerts,
        "urlopen",
        lambda *_args, **_kwargs: _BlockingResponse(entered, release),
    )
    webhook = alerts.WebhookAlertSink("https://example.invalid/hook")
    manager = alerts.AlertManager([webhook])
    manager.critical("halt")
    assert entered.wait(timeout=1)
    manager.critical("queued-1")
    manager.critical("queued-2")

    started = monotonic()
    manager.close(timeout_seconds=0.01)
    assert monotonic() - started < 0.2
    assert webhook.pending_count == 0
    assert webhook.dropped_count == 2
    release.set()
