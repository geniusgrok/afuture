from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from afuture.alerts import AlertManager, MemoryAlertSink
from afuture.heartbeat import (
    HeartbeatIntegrityError,
    HeartbeatWriter,
    load_heartbeat,
)
from afuture.watchdog import evaluate_heartbeat


def _facts(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "mode": "live",
        "policy": "stress90",
        "canonical_runtime_digest": "a" * 64,
        "account_identity_digest": "b" * 64,
        "ctp_trading_day": "20260828",
        "broker_connection_state": "ready",
        "last_account_snapshot_age_seconds": 1.0,
        "last_complete_position_snapshot_age_seconds": 1.5,
        "max_required_quote_age_seconds": 0.5,
        "critical_queue": {
            "critical_backlog": 0,
            "critical_backlog_streak": 0,
            "critical_enqueued": 8,
            "critical_delivered": 8,
        },
        "active_order_count": 0,
        "runtime_mode": "RUNNING",
        "kill_switch": False,
        "reconciled": True,
        "generic_state_sequence": 7,
        "generic_state_checksum": "c" * 64,
        "policy_state_sequence": 11,
        "policy_state_checksum": "d" * 64,
        "risk_overlay_digest": "e" * 64,
        "deployment_identity_digest": "f" * 64,
        "last_successful_cycle_utc": "2026-08-28T00:00:00+00:00",
        "last_error_category": "",
    }
    values.update(overrides)
    return values


def test_heartbeat_schema_checksum_and_process_binding(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    mono = iter((100.0, 101.0))
    writer = HeartbeatWriter(
        path,
        interval_seconds=5,
        process_uuid="11111111-1111-4111-8111-111111111111",
        process_start_utc="2026-08-28T00:00:00+00:00",
        monotonic_clock=lambda: next(mono),
        utc_clock=lambda: datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    assert writer.write(_facts(), force=True) is True
    payload = load_heartbeat(path)
    assert payload["kind"] == "afuture.runtime.heartbeat"
    assert payload["schema_version"] == 1
    assert UUID(str(payload["process_uuid"]))
    assert payload["checksum"]
    assert payload["process_status"] == "running"


def test_heartbeat_atomic_replace_preserves_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    writer = HeartbeatWriter(path, interval_seconds=1)
    assert writer.write(_facts(), force=True)
    first = path.stat().st_ino
    assert writer.write(_facts(active_order_count=2), force=True)
    second = path.stat().st_ino
    assert first != second
    assert load_heartbeat(path)["active_order_count"] == 2


def test_heartbeat_rejects_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "victim.json"
    target.write_text("victim", encoding="utf-8")
    path = tmp_path / "heartbeat.json"
    path.symlink_to(target)
    writer = HeartbeatWriter(path, interval_seconds=1)
    with pytest.raises(HeartbeatIntegrityError, match="symlink"):
        writer.write(_facts(), force=True)
    assert target.read_text(encoding="utf-8") == "victim"


def test_heartbeat_write_is_throttled(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    now = [100.0]
    writer = HeartbeatWriter(path, interval_seconds=5, monotonic_clock=lambda: now[0])
    assert writer.write(_facts(), force=True)
    now[0] = 102.0
    assert writer.write(_facts()) is False
    now[0] = 105.0
    assert writer.write(_facts()) is True


def test_heartbeat_clean_stopped_marker_binds_uuid_and_state(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    process_uuid = "22222222-2222-4222-8222-222222222222"
    writer = HeartbeatWriter(path, interval_seconds=1, process_uuid=process_uuid)
    assert writer.write(_facts(), force=True)
    assert writer.write_stopped(_facts(), final_state_checksum="9" * 64, clean=True)
    payload = load_heartbeat(path)
    assert payload["process_status"] == "stopped"
    assert payload["clean_shutdown"] is True
    assert payload["process_uuid"] == process_uuid
    assert payload["final_state_checksum"] == "9" * 64


def test_heartbeat_write_failure_alerts_without_raising(tmp_path: Path) -> None:
    parent = tmp_path / "not-a-directory"
    parent.write_text("x", encoding="utf-8")
    sink = MemoryAlertSink()
    writer = HeartbeatWriter(
        parent / "heartbeat.json",
        interval_seconds=1,
        alert_manager=AlertManager([sink]),
    )
    assert writer.write(_facts(), force=True) is False
    assert sink.events
    assert sink.events[-1]["level"] == "CRITICAL"


def test_heartbeat_rejects_secret_like_fields(tmp_path: Path) -> None:
    writer = HeartbeatWriter(tmp_path / "heartbeat.json", interval_seconds=1)
    with pytest.raises(HeartbeatIntegrityError, match="secret"):
        writer.write(_facts(ctp_password="should-never-be-written"), force=True)


def _healthy_heartbeat(tmp_path: Path) -> dict[str, object]:
    writer = HeartbeatWriter(
        tmp_path / "heartbeat.json",
        interval_seconds=1,
        process_start_utc="2026-08-28T00:00:00+00:00",
        utc_clock=lambda: datetime(2026, 8, 28, 0, 0, 5, tzinfo=timezone.utc),
    )
    assert writer.write(_facts(), force=True)
    return load_heartbeat(tmp_path / "heartbeat.json")


def test_watchdog_healthy_payload_passes(tmp_path: Path) -> None:
    heartbeat = _healthy_heartbeat(tmp_path)
    result = evaluate_heartbeat(
        heartbeat,
        now_utc=datetime(2026, 8, 28, 0, 0, 6, tzinfo=timezone.utc),
        max_age_seconds=10,
        expected_deployment_digest="f" * 64,
        expected_risk_overlay_digest="e" * 64,
        process_run_clean=True,
    )
    assert result.passed
    assert result.to_dict()["passed"] is True


@pytest.mark.parametrize(
    ("mutation", "expected_check"),
    [
        ({"broker_connection_state": "disconnected"}, "broker_health"),
        ({"last_account_snapshot_age_seconds": 30.0}, "account_snapshot_freshness"),
        ({"last_complete_position_snapshot_age_seconds": 30.0}, "position_snapshot_freshness"),
        ({"max_required_quote_age_seconds": 30.0}, "quote_freshness"),
        ({"critical_queue": {"critical_backlog": 20, "critical_backlog_streak": 4}}, "critical_backlog"),
        ({"deployment_identity_digest": "1" * 64}, "deployment_identity"),
        ({"risk_overlay_digest": "2" * 64}, "risk_overlay_identity"),
        ({"runtime_mode": "HALTED", "kill_switch": True, "last_error_category": "risk"}, "runtime_state"),
    ],
)
def test_watchdog_rejects_unhealthy_runtime_facts(
    tmp_path: Path,
    mutation: dict[str, object],
    expected_check: str,
) -> None:
    heartbeat = _healthy_heartbeat(tmp_path)
    heartbeat.update(mutation)
    result = evaluate_heartbeat(
        heartbeat,
        now_utc=datetime(2026, 8, 28, 0, 0, 6, tzinfo=timezone.utc),
        max_age_seconds=10,
        expected_deployment_digest="f" * 64,
        expected_risk_overlay_digest="e" * 64,
        process_run_clean=True,
    )
    assert not result.passed
    assert expected_check in {item.name for item in result.checks if not item.passed}


def test_watchdog_rejects_stale_heartbeat(tmp_path: Path) -> None:
    heartbeat = _healthy_heartbeat(tmp_path)
    result = evaluate_heartbeat(
        heartbeat,
        now_utc=datetime(2026, 8, 28, 0, 1, tzinfo=timezone.utc),
        max_age_seconds=10,
        expected_deployment_digest="f" * 64,
        expected_risk_overlay_digest="e" * 64,
        process_run_clean=True,
    )
    assert not result.passed
    assert "heartbeat_freshness" in {item.name for item in result.checks if not item.passed}


def test_watchdog_rejects_clean_and_unclean_stopped_processes(tmp_path: Path) -> None:
    for clean in (True, False):
        writer = HeartbeatWriter(tmp_path / f"heartbeat-{clean}.json", interval_seconds=1)
        assert writer.write_stopped(_facts(), final_state_checksum="7" * 64, clean=clean)
        heartbeat = load_heartbeat(tmp_path / f"heartbeat-{clean}.json")
        result = evaluate_heartbeat(
            heartbeat,
            now_utc=datetime.now(timezone.utc),
            max_age_seconds=60,
            expected_deployment_digest="f" * 64,
            expected_risk_overlay_digest="e" * 64,
            process_run_clean=clean,
        )
        assert not result.passed
        assert "process_status" in {item.name for item in result.checks if not item.passed}


def test_watchdog_rejects_unclean_restart_fence(tmp_path: Path) -> None:
    heartbeat = _healthy_heartbeat(tmp_path)
    result = evaluate_heartbeat(
        heartbeat,
        now_utc=datetime(2026, 8, 28, 0, 0, 6, tzinfo=timezone.utc),
        max_age_seconds=10,
        expected_deployment_digest="f" * 64,
        expected_risk_overlay_digest="e" * 64,
        process_run_clean=False,
    )
    assert not result.passed
    assert "process_run" in {item.name for item in result.checks if not item.passed}
