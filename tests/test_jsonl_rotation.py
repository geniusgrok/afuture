import json
from pathlib import Path

from afuture.alerts import FileAlertSink
from afuture.journal import AuditJournal
from afuture.jsonl import RotatingJsonlWriter


def _read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_rotating_writer_preserves_whole_json_lines_and_backup_limit(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    writer = RotatingJsonlWriter(path, max_bytes=20, backup_count=2)

    writer.write_line('{"event":1}')
    writer.write_line('{"event":2}')
    writer.write_line('{"event":3}')
    writer.write_line('{"event":4}')

    assert _read_rows(path) == [{"event": 4}]
    assert _read_rows(tmp_path / "events.jsonl.1") == [{"event": 3}]
    assert _read_rows(tmp_path / "events.jsonl.2") == [{"event": 2}]
    assert not (tmp_path / "events.jsonl.3").exists()


def test_audit_and_alert_files_use_bounded_rotation(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    alert_path = tmp_path / "alerts.jsonl"
    journal = AuditJournal(audit_path, max_bytes=80, backup_count=1)
    alerts = FileAlertSink(alert_path, max_bytes=40, backup_count=1)

    journal.record("first", {"value": "x" * 30})
    journal.record("second", {"value": "y" * 30})
    alerts.send({"level": "WARN", "message": "x" * 20})
    alerts.send({"level": "ERROR", "message": "y" * 20})

    assert _read_rows(audit_path)[0]["event_type"] == "second"
    assert _read_rows(tmp_path / "audit.jsonl.1")[0]["event_type"] == "first"
    assert _read_rows(alert_path)[0]["level"] == "ERROR"
    assert _read_rows(tmp_path / "alerts.jsonl.1")[0]["level"] == "WARN"
