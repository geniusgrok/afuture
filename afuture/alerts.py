"""生产告警通道。关键风险事件应独立于普通运行日志保存和通知。"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import stat
from collections.abc import Sequence
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, time
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

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


class AlertDeliveryError(RuntimeError):
    """Critical notification could not be durably queued or its spool is untrusted."""


class _RejectWebhookRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Reject before urllib can forward notification data to another origin.
        raise AlertDeliveryError("notification redirects are not an approved destination")


urlopen = build_opener(_RejectWebhookRedirects()).open


class WebhookAlertSink:
    """Durable, bounded, at-least-once webhook delivery off the trading thread.

    Failed and in-flight messages survive process death. HTTP acceptance is not
    proof that a person read the event. An ambiguous timeout may resend the same
    event_id; the receiver should use the Idempotency-Key to deduplicate it.
    """

    _MAX_EVENT_BYTES = 16_384
    _MAX_DATABASE_BYTES = 32 * 1024 * 1024
    _MAX_RETRY_SECONDS = 60.0
    _DEDUP_SECONDS = 60.0
    _RETAIN_ACCEPTED_SECONDS = 32 * 86400

    def __init__(
        self,
        url: str,
        timeout_seconds: float = 3.0,
        *,
        spool_path: str | Path,
        max_queue: int = 2048,
        max_attempts: int = 6,
        start_worker: bool = True,
    ) -> None:
        destination = urlsplit(url)
        if (
            destination.scheme != "https"
            or not destination.netloc
            or destination.username
            or destination.password
            or destination.fragment
        ):
            raise ValueError("webhook requires an HTTPS URL without userinfo or fragment")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30
        ):
            raise ValueError("webhook timeout must be finite and between 0 and 30 seconds")
        if type(max_queue) is not int or not 1 <= max_queue <= 4096:
            raise ValueError("webhook queue bound must be an integer in [1,4096]")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 12:
            raise ValueError("webhook attempt bound must be an integer in [1,12]")
        self.url = url
        self.timeout_seconds = float(timeout_seconds)
        self.max_queue = max_queue
        self.max_attempts = max_attempts
        self.path = Path(spool_path)
        self._state_lock = Lock()
        self._closed = Event()
        self._wake = Event()
        self._db_closed = False
        self._last_diagnostics: dict[str, object] = {}
        self._worker_error_category = ""
        self._worker: Thread | None = None
        self._open_spool()
        if start_worker:
            self._worker = Thread(target=self._run, name="afuture-alert-webhook", daemon=True)
            self._worker.start()

    def _open_spool(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        created = False
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            metadata = self.path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise AlertDeliveryError("alert spool must be a regular, unlinked file") from None
        else:
            os.close(fd)
            created = True
        self._db = sqlite3.connect(self.path, timeout=0.1, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            pages = self._MAX_DATABASE_BYTES // int(
                self._db.execute("PRAGMA page_size").fetchone()[0]
            )
            self._db.execute(f"PRAGMA max_page_count={pages}")
            if created:
                with self._db:
                    self._db.execute("CREATE TABLE destination (digest TEXT NOT NULL)")
                    self._db.execute(
                        "INSERT INTO destination VALUES (?)",
                        (sha256(self.url.encode()).hexdigest(),),
                    )
                    self._db.execute("""CREATE TABLE outbox (
                        event_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                        payload TEXT NOT NULL, created REAL NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending', claim TEXT NOT NULL DEFAULT '',
                        lease_until REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                        response_code INTEGER, accepted_at REAL, repeats INTEGER NOT NULL DEFAULT 0
                    )""")
                    self._db.execute("CREATE INDEX alert_fingerprint ON outbox(fingerprint)")
                    self._db.execute("PRAGMA user_version=1")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            integrity = self._db.execute("PRAGMA quick_check").fetchone()[0]
            destinations = self._db.execute("SELECT digest FROM destination").fetchall()
            if (
                version != 1
                or integrity != "ok"
                or len(destinations) != 1
                or destinations[0][0] != sha256(self.url.encode()).hexdigest()
            ):
                raise AlertDeliveryError("alert spool identity/schema/integrity mismatch")
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(outbox)")}
            if columns != {
                "event_id",
                "fingerprint",
                "payload",
                "created",
                "attempts",
                "next_try",
                "status",
                "claim",
                "lease_until",
                "last_error",
                "response_code",
                "accepted_at",
                "repeats",
            }:
                raise AlertDeliveryError("alert spool fields are invalid")
        except Exception:
            self._db.close()
            self._db_closed = True
            raise

    def send(self, event: dict[str, object]) -> None:
        now = time()
        body = json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        if len(body.encode()) > self._MAX_EVENT_BYTES:
            raise AlertDeliveryError("alert exceeds durable payload bound")
        incident = {key: value for key, value in event.items() if key != "timestamp"}
        fingerprint = sha256(
            json.dumps(incident, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
        with self._state_lock:
            if self._closed.is_set():
                raise AlertDeliveryError("webhook alert sink is closed")
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                existing = self._db.execute(
                    "SELECT event_id FROM outbox WHERE fingerprint=? "
                    "AND (status!='accepted' OR accepted_at>=?) ORDER BY created DESC LIMIT 1",
                    (fingerprint, now - self._DEDUP_SECONDS),
                ).fetchone()
                if existing is not None:
                    self._db.execute(
                        "UPDATE outbox SET repeats=repeats+1 WHERE event_id=?", (existing[0],)
                    )
                else:
                    count = self._db.execute(
                        "SELECT COUNT(*) FROM outbox WHERE status!='accepted'"
                    ).fetchone()[0]
                    if count >= self.max_queue:
                        raise AlertDeliveryError(
                            "durable alert spool is full; unacknowledged events retained"
                        )
                    self._db.execute(
                        "DELETE FROM outbox WHERE status='accepted' AND accepted_at<?",
                        (now - self._RETAIN_ACCEPTED_SECONDS,),
                    )
                    self._db.execute(
                        "INSERT INTO outbox(event_id,fingerprint,payload,created,next_try) VALUES(?,?,?,?,?)",
                        (uuid4().hex, fingerprint, body, now, now),
                    )
        self._wake.set()

    def diagnostics(self) -> dict[str, object]:
        with self._state_lock:
            return self._diagnostics_locked()

    def _diagnostics_locked(self) -> dict[str, object]:
        if self._db_closed:
            return dict(self._last_diagnostics)
        counts = dict(
            self._db.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status").fetchall()
        )
        accepted = self._db.execute(
            "SELECT MAX(accepted_at) FROM outbox WHERE status='accepted'"
        ).fetchone()[0]
        return {
            "pending_count": counts.get("pending", 0) + counts.get("inflight", 0),
            "failed_count": counts.get("failed", 0),
            "worker_error_category": self._worker_error_category,
            "last_http_accepted_utc": None
            if accepted is None
            else datetime.fromtimestamp(accepted, timezone.utc).isoformat(),
            "delivery_evidence_level": "http_accepted_only",
            "human_read_verified": False,
        }

    @property
    def pending_count(self) -> int:
        value = self.diagnostics()["pending_count"]
        if type(value) is not int:
            raise AlertDeliveryError("alert pending count is invalid")
        return value

    def _deliver(self, event_id: str, payload: str) -> int:
        event = json.loads(payload)
        event["event_id"] = event_id
        request = Request(
            self.url,
            data=json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Idempotency-Key": event_id},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            if response.geturl() != self.url or not 200 <= response.status < 300:
                raise AlertDeliveryError("notification endpoint did not acknowledge this request")
            response.read(1)
            return int(response.status)

    def deliver_one(self, *, now: float | None = None) -> bool:
        """Deliver at most one due event; no Broker or trading state is reachable here."""
        timestamp = time() if now is None else now
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
        ):
            raise ValueError("notification clock must be finite")
        claim = uuid4().hex
        horizon = self._MAX_RETRY_SECONDS + self.timeout_seconds + 5
        with self._state_lock:
            if self._closed.is_set():
                return False
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT * FROM outbox WHERE "
                    "(status='pending' AND (next_try<=? OR next_try>?)) OR "
                    "(status='inflight' AND (lease_until<=? OR lease_until>?)) "
                    "ORDER BY created LIMIT 1",
                    (timestamp, timestamp + horizon, timestamp, timestamp + horizon),
                ).fetchone()
                if row is None:
                    return False
                # An expired in-flight claim is an ambiguous delivery, not a reason
                # to discard the event or reset the retry budget on process restart.
                if row["attempts"] >= self.max_attempts:
                    self._db.execute(
                        "UPDATE outbox SET status='failed',claim='' WHERE event_id=?",
                        (row["event_id"],),
                    )
                    logger.error(
                        "critical notification retry budget exhausted; event_id=%s", row["event_id"]
                    )
                    return True
                self._db.execute(
                    "UPDATE outbox SET status='inflight',attempts=attempts+1,claim=?,lease_until=? WHERE event_id=?",
                    (claim, timestamp + self.timeout_seconds + 5, row["event_id"]),
                )
        error = ""
        response_code = None
        try:
            response_code = self._deliver(row["event_id"], row["payload"])
        except Exception as exc:
            error = type(exc).__name__  # URLs may contain webhook credentials.
        attempts = row["attempts"] + 1
        status = (
            "accepted" if not error else ("failed" if attempts >= self.max_attempts else "pending")
        )
        with self._state_lock, self._db:
            self._db.execute(
                "UPDATE outbox SET status=?,next_try=?,claim='',lease_until=0,last_error=?,"
                "response_code=?,accepted_at=? WHERE event_id=? AND claim=?",
                (
                    status,
                    timestamp + min(self._MAX_RETRY_SECONDS, 2 ** (attempts - 1)),
                    error,
                    response_code,
                    time() if not error else None,
                    row["event_id"],
                    claim,
                ),
            )
        if status == "failed":
            logger.error(
                "critical notification failed; event_id=%s attempts=%d category=%s",
                row["event_id"],
                attempts,
                error,
            )
        return True

    def _run(self) -> None:
        try:
            while not self._closed.is_set():
                try:
                    delivered = self.deliver_one()
                    with self._state_lock:
                        self._worker_error_category = ""
                except Exception as exc:
                    category = type(exc).__name__
                    with self._state_lock:
                        changed = self._worker_error_category != category
                        self._worker_error_category = category
                    if changed:
                        logger.error(
                            "alert spool worker failed (%s); pending evidence retained", category
                        )
                    delivered = False
                if not delivered:
                    self._wake.wait(timeout=0.25)
                    self._wake.clear()
        finally:
            self._close_database()

    def _close_database(self) -> None:
        with self._state_lock:
            if not self._db_closed:
                self._last_diagnostics = self._diagnostics_locked()
                self._db.close()
                self._db_closed = True

    def close(self, *, timeout_seconds: float = 1.0) -> None:
        self._closed.set()
        self._wake.set()
        if self._worker is None:
            self._close_database()
        else:
            self._worker.join(timeout=max(0.0, timeout_seconds))
        # A blocked network call may finish after this deadline. Queued and
        # claimed rows remain durable; a new process recovers expired claims.


class AlertManager:
    """把同一个风险事件广播到多个告警通道；单个通道失败不影响其他通道。"""

    def __init__(self, sinks: Sequence[AlertSink] | None = None) -> None:
        self.sinks = list(sinks or [])
        self.persistence_failures = 0

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
                self.persistence_failures += 1
                logger.error(
                    "alert delivery failed for sink %s (%s)",
                    type(sink).__name__,
                    type(exc).__name__,
                )

    def diagnostics(self) -> dict[str, object]:
        deliveries = []
        for sink in self.sinks:
            read = getattr(sink, "diagnostics", None)
            if callable(read):
                try:
                    deliveries.append(read())
                except Exception as exc:
                    deliveries.append({"error_category": type(exc).__name__})
        return {
            "persistence_failures": self.persistence_failures,
            "remote_channel_configured": bool(deliveries),
            "deliveries": deliveries,
        }

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
