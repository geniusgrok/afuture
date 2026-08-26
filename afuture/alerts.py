"""生产告警通道。关键风险事件应独立于普通运行日志保存和通知。"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue
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
    """Bounded asynchronous JSON webhook with no network I/O on caller threads."""

    def __init__(
        self,
        url: str,
        timeout_seconds: float = 3.0,
        *,
        max_queue: int = 256,
    ) -> None:
        if not url:
            raise ValueError("webhook URL is required")
        if timeout_seconds <= 0.0:
            raise ValueError("webhook timeout must be positive")
        if isinstance(max_queue, bool) or not isinstance(max_queue, int) or max_queue < 1:
            raise ValueError("webhook queue bound must be a positive integer")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._queue: Queue[dict[str, object]] = Queue(maxsize=max_queue)
        self._closed = Event()
        self._discard_pending = Event()
        self._state_lock = Lock()
        self._dropped_count = 0
        self._worker = Thread(
            target=self._run,
            name="afuture-alert-webhook",
            daemon=True,
        )
        self._worker.start()

    def send(self, event: dict[str, object]) -> None:
        with self._state_lock:
            if self._closed.is_set():
                raise RuntimeError("webhook alert sink is closed")
            try:
                self._queue.put_nowait(dict(event))
            except Full:
                self._dropped_count += 1
                dropped = self._dropped_count
                if dropped == 1 or dropped & (dropped - 1) == 0:
                    logger.error(
                        "webhook alert queue full; dropped=%d; local alert evidence remains authoritative",
                        dropped,
                    )

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    @property
    def dropped_count(self) -> int:
        with self._state_lock:
            return self._dropped_count

    def _deliver(self, event: dict[str, object]) -> None:
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            response.read(1)

    def _run(self) -> None:
        while not self._closed.is_set() or not self._queue.empty():
            try:
                event = self._queue.get(timeout=0.05)
            except Empty:
                continue
            if self._discard_pending.is_set():
                self._queue.task_done()
                return
            try:
                self._deliver(event)
            except Exception as exc:
                logger.warning(
                    "alert delivery failed for sink %s (%s)",
                    type(self).__name__,
                    type(exc).__name__,
                )
            finally:
                self._queue.task_done()

    def close(self, *, timeout_seconds: float = 1.0) -> None:
        with self._state_lock:
            self._closed.set()
        self._worker.join(timeout=max(0.0, timeout_seconds))
        if not self._worker.is_alive():
            return
        self._discard_pending.set()
        discarded = 0
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                break
            else:
                discarded += 1
                self._queue.task_done()
        if discarded:
            with self._state_lock:
                self._dropped_count += discarded
            logger.error(
                "webhook close deadline expired; discarded=%d; local alert evidence remains authoritative",
                discarded,
            )


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
