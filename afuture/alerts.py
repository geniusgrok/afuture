"""生产告警通道。关键风险事件应独立于普通运行日志保存和通知。"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol
from urllib.request import Request, urlopen

from .jsonl import DEFAULT_JSONL_BACKUP_COUNT, DEFAULT_JSONL_MAX_BYTES, RotatingJsonlWriter

logger = logging.getLogger(__name__)


class AlertSink(Protocol):
    """Minimal delivery contract shared by local and remote alert channels."""

    def send(self, event: dict[str, object]) -> None: ...


class MemoryAlertSink:
    """测试和嵌入场景使用的内存告警接收器。"""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def send(self, event: dict[str, object]) -> None:
        self.events.append(dict(event))


class FileAlertSink:
    """追加写 JSONL，确保即使外部通知失败仍保留本地证据。"""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_JSONL_MAX_BYTES,
        backup_count: int = DEFAULT_JSONL_BACKUP_COUNT,
    ) -> None:
        self.path = Path(path)
        self._writer = RotatingJsonlWriter(
            self.path,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )

    def send(self, event: dict[str, object]) -> None:
        self._writer.write_line(json.dumps(event, ensure_ascii=False, separators=(",", ":")))


class WebhookAlertSink:
    """Durable at-least-once JSON webhook; no network I/O on the trading thread."""

    def __init__(
        self,
        url: str,
        timeout_seconds: float = 3.0,
        *,
        outbox_path: str | Path,
        max_pending: int = 4096,
    ) -> None:
        from .alert_outbox import AlertOutbox

        if not url or not 0 < timeout_seconds <= 30:
            raise ValueError("webhook URL and bounded positive timeout are required")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.outbox = AlertOutbox(outbox_path, url, max_pending=max_pending)
        self._closed = Event()
        self._wake = Event()
        self._state_lock = Lock()
        self._worker_error = ""
        self._worker = Thread(target=self._run, name="afuture-alert-webhook", daemon=True)
        self._worker.start()

    def send(self, event: dict[str, object]) -> None:
        with self._state_lock:
            if self._closed.is_set():
                raise RuntimeError("webhook alert sink is closed")
            self.outbox.enqueue(dict(event))
        self._wake.set()

    @property
    def pending_count(self) -> int:
        pending = self.outbox.status()["pending"]
        if not isinstance(pending, int):
            raise RuntimeError("notification pending count is invalid")
        return pending

    def delivery_status(self) -> dict[str, object]:
        status = self.outbox.status()
        if self._worker_error:
            status["error"] = self._worker_error
        return status

    def _deliver(self, event_id: str, event: dict[str, object]) -> int:
        body = json.dumps(
            {**event, "event_id": event_id}, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": event_id,
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            status = int(response.status)
            response.read(1)
        return status

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                item = self.outbox.next_event()
                if item is None:
                    self._wake.wait(timeout=0.5)
                    self._wake.clear()
                    continue
                event_id, event, attempt = item
                try:
                    status = self._deliver(event_id, event)
                    self.outbox.accepted(event_id, status)
                except Exception as exc:
                    self.outbox.failed(event_id, attempt, type(exc).__name__)
                    logger.warning(
                        "notification delivery failed (%s); event retained, attempt=%d",
                        type(exc).__name__,
                        attempt,
                    )
            except Exception as exc:
                # Storage failure is visible to supervision, never silently discarded.
                self._worker_error = f"notification worker failure ({type(exc).__name__})"
                logger.error(self._worker_error)
                return

    def close(self, *, timeout_seconds: float = 1.0) -> None:
        with self._state_lock:
            self._closed.set()
        self._wake.set()
        self._worker.join(timeout=max(0.0, timeout_seconds))
        # Pending rows are deliberately retained across close, timeout and process death.


class AlertManager:
    """把同一个风险事件广播到多个告警通道；单个通道失败不影响其他通道。"""

    def __init__(self, sinks: Sequence[AlertSink] | None = None) -> None:
        self.sinks = list(sinks or [])

    def emit(self, level: str, message: str, details: dict | None = None) -> None:
        event: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
            "details": details or {},
        }
        for sink in self.sinks:
            try:
                sink.send(event)
            except Exception as exc:
                # 告警故障不能阻止风控动作本身，但必须留下可诊断证据。
                logger.warning(
                    "alert delivery failed for sink %s (%s)",
                    type(sink).__name__,
                    type(exc).__name__,
                )

    def delivery_status(self) -> list[dict[str, object]]:
        return [sink.delivery_status() for sink in self.sinks if isinstance(sink, WebhookAlertSink)]

    def critical(self, message: str, details: dict | None = None) -> None:
        self.emit("CRITICAL", message, details)

    def close(self, *, timeout_seconds: float = 1.0) -> None:
        deadline = monotonic() + max(0.0, timeout_seconds)
        for sink in self.sinks:
            closer = getattr(sink, "close", None)
            if not callable(closer):
                continue
            try:
                closer(timeout_seconds=max(0.0, deadline - monotonic()))
            except Exception as exc:
                logger.warning(
                    "alert sink close failed for %s (%s)",
                    type(sink).__name__,
                    type(exc).__name__,
                )
