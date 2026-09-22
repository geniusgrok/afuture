"""Durable, bounded notification delivery records; never trading/account state."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from time import time

from .durable_file_creation import canonical_file_path
from .durable_json import canonical_json_bytes


class AlertOutboxError(RuntimeError):
    """A notification cannot be durably retained or safely acknowledged."""


class AlertOutbox:
    def __init__(self, path: str | Path, endpoint: str, *, max_pending: int = 4096) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 50000:
            raise ValueError("outbox pending bound must be in 1..50000")
        self.path = canonical_file_path(path)
        self.endpoint_digest = sha256(endpoint.encode()).hexdigest()
        self.max_pending = max_pending
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._error = ""
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise AlertOutboxError("notification outbox schema is unsupported")
            if version == 0:
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                    raise AlertOutboxError("schema-less notification outbox is not empty")
                db.executescript("""
                    CREATE TABLE identity(endpoint TEXT NOT NULL);
                    CREATE TABLE events(
                        id TEXT PRIMARY KEY, payload TEXT NOT NULL, dedupe TEXT NOT NULL,
                        created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                        next_due REAL NOT NULL DEFAULT 0, delivered REAL,
                        http_status INTEGER, last_error TEXT NOT NULL DEFAULT '',
                        duplicates INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX due ON events(delivered, next_due, created);
                    CREATE INDEX duplicate_event ON events(dedupe, created);
                    PRAGMA user_version=1;
                """)
                db.execute("INSERT INTO identity VALUES (?)", (self.endpoint_digest,))
            if db.execute("SELECT endpoint FROM identity").fetchall() != [(self.endpoint_digest,)]:
                raise AlertOutboxError("notification endpoint changed for this outbox")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise AlertOutboxError("notification outbox integrity check failed")
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self):
        # SQLite side files must live in the protected runtime, not a symlink target.
        for path in (self.path, Path(str(self.path) + "-journal")):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise AlertOutboxError("notification outbox path is not a regular file")
        db = sqlite3.connect(self.path, timeout=0.25)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA max_page_count=32768")  # bounded to ~128 MiB at default page size
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def enqueue(self, event: dict[str, object]) -> str:
        encoded = canonical_json_bytes(event)
        if len(encoded) > 16384:
            self._error = "notification event exceeds durable payload bound"
            raise AlertOutboxError(self._error)
        digest = sha256(encoded).hexdigest()
        # Suppress repeated reports of the same still-pending failure for one minute.
        semantic = {key: value for key, value in event.items() if key != "timestamp"}
        dedupe = sha256(canonical_json_bytes(semantic)).hexdigest()
        now = time()
        try:
            with self._connection() as db:
                same = db.execute("SELECT id FROM events WHERE id=?", (digest,)).fetchone()
                if same:
                    return str(same[0])
                pending = db.execute(
                    "SELECT id FROM events WHERE dedupe=? AND delivered IS NULL AND created>=? ORDER BY created DESC LIMIT 1",
                    (dedupe, now - 60),
                ).fetchone()
                if pending:
                    db.execute("UPDATE events SET duplicates=duplicates+1 WHERE id=?", pending)
                    return str(pending[0])
                db.execute(
                    "DELETE FROM events WHERE delivered IS NOT NULL AND delivered<?",
                    (now - 32 * 86400,),
                )
                count = db.execute(
                    "SELECT count(*) FROM events WHERE delivered IS NULL"
                ).fetchone()[0]
                if count >= self.max_pending:
                    raise AlertOutboxError(
                        "notification outbox is full; undelivered events retained"
                    )
                db.execute(
                    "INSERT INTO events(id,payload,dedupe,created) VALUES(?,?,?,?)",
                    (digest, encoded.decode(), dedupe, now),
                )
        except (sqlite3.Error, OSError, AlertOutboxError) as exc:
            self._error = f"notification durability failure ({type(exc).__name__})"
            raise AlertOutboxError(self._error) from exc
        return digest

    def next_event(self):
        with self._connection() as db:
            row = db.execute(
                "SELECT id,payload,attempts FROM events WHERE delivered IS NULL AND attempts<8 AND next_due<=? ORDER BY created,id LIMIT 1",
                (time(),),
            ).fetchone()
            if row is None:
                return None
            event_id, payload, attempts = row
            # Persist the attempt before external I/O. A process crash can redeliver
            # this stable ID, but cannot erase the unsent event or claim delivery.
            db.execute(
                "UPDATE events SET attempts=attempts+1,next_due=? WHERE id=?",
                (time() + 30, event_id),
            )
            return str(event_id), json.loads(payload), int(attempts) + 1

    def failed(self, event_id: str, attempt: int, error: str) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE events SET next_due=?,last_error=? WHERE id=? AND delivered IS NULL",
                (time() + min(300, 2**attempt), error, event_id),
            )

    def accepted(self, event_id: str, status: int) -> None:
        if not 200 <= status < 300:
            raise AlertOutboxError("notification endpoint did not accept HTTP request")
        with self._connection() as db:
            db.execute(
                "UPDATE events SET delivered=?,http_status=?,last_error='' WHERE id=? AND delivered IS NULL",
                (time(), status, event_id),
            )

    def status(self) -> dict[str, object]:
        with self._connection() as db:
            pending, exhausted, oldest = db.execute(
                "SELECT count(*),coalesce(sum(attempts>=8),0),min(created) FROM events WHERE delivered IS NULL"
            ).fetchone()
            delivered = db.execute(
                "SELECT count(*) FROM events WHERE delivered IS NOT NULL"
            ).fetchone()[0]
        return {
            "pending": pending,
            "retry_exhausted": exhausted,
            "oldest_pending_utc_epoch": oldest,
            "http_accepted": delivered,
            "delivery_evidence": "http_acceptance_not_human_read",
            "error": self._error or ("notification retries exhausted" if exhausted else ""),
        }
