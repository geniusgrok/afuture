from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from afuture.directional_activity import (
    ContractActivity,
    DirectionalActivitySnapshot,
    DirectionalActivityStore,
)
from afuture.models import AccountSnapshot, ContractPosition, ContractSpec, FeeSpec, RuntimeMode
from afuture.operations import build_doctor_report, build_local_status
from afuture.state import RuntimeState, StateStore


def _config(tmp_path: Path, *, directional: bool = False):
    spec = ContractSpec("m2609", "DCE", 10, 1, 0.12, 0.12, FeeSpec(open_fixed=1))
    return SimpleNamespace(
        state_path=str(tmp_path / "state.json"),
        log_path=str(tmp_path / "afuture.log"),
        report_path=str(tmp_path / "report.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
        alert_path=str(tmp_path / "alerts.jsonl"),
        contracts={spec.symbol: spec},
        directional=SimpleNamespace(enabled=directional),
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(500_000, 500_000, 440_000, 60_000, 0, 0, "20260825")


def _checks(report) -> dict[str, bool]:
    return {item.name: item.passed for item in report.checks}


def test_local_status_reports_current_and_previous_verified_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.state_path)
    store.save(RuntimeState(trading_day="20260824", last_order_id="old"))
    store.save(
        RuntimeState(
            trading_day="20260825",
            reconciled=True,
            metadata_verified=True,
            last_order_id="new",
        )
    )

    report = build_local_status(config, min_free_bytes=1)
    payload = report.to_dict()

    assert report.passed
    assert payload["facts"]["state"]["trading_day"] == "20260825"
    assert payload["facts"]["state"]["last_order_id"] == "new"
    assert payload["facts"]["previous_state"]["trading_day"] == "20260824"
    assert payload["facts"]["paths"]["audit"]["size_bytes"] == 0


def test_local_status_fails_closed_on_corrupt_current_but_exposes_previous(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.state_path)
    store.save(RuntimeState(trading_day="20260824"))
    store.save(RuntimeState(trading_day="20260825"))
    store.path.write_text("{broken", encoding="utf-8")

    report = build_local_status(config, min_free_bytes=1)

    assert not report.passed
    assert not _checks(report)["state_integrity"]
    assert report.facts["state"]["valid"] is False
    assert report.facts["previous_state"]["valid"] is True


def test_doctor_passes_verified_flat_preflight(tmp_path: Path) -> None:
    config = _config(tmp_path)
    StateStore(config.state_path).save(
        RuntimeState(
            runtime_mode=RuntimeMode.RUNNING.value,
            reconciled=True,
            metadata_verified=True,
            trading_day="20260825",
        )
    )

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog_count=20,
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    assert report.passed
    assert all(_checks(report).values())
    assert report.facts["broker"]["orders_sent"] == 0


def test_doctor_rejects_kill_switch_active_orders_and_position_mismatch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    StateStore(config.state_path).save(
        RuntimeState(
            kill_switch=True,
            runtime_mode=RuntimeMode.HALTED.value,
            reconciled=False,
            metadata_verified=False,
            positions=[],
        )
    )
    remote = [ContractPosition("m2609", "DCE", long_today=1, long_price=3000)]

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=remote,
        active_order_count=1,
        catalog_count=20,
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )
    checks = _checks(report)

    assert not report.passed
    assert not checks["kill_switch_clear"]
    assert not checks["runtime_mode_running"]
    assert not checks["no_active_orders"]
    assert not checks["position_reconciliation"]
    assert not checks["persisted_safety_gates"]


def test_doctor_requires_completed_directional_activity_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, directional=True)

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog_count=20,
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    assert not _checks(report)["directional_activity_ready"]


def test_doctor_rejects_internally_inconsistent_directional_activity(tmp_path: Path) -> None:
    config = _config(tmp_path, directional=True)
    activity_path = tmp_path / "directional_activity.json"
    DirectionalActivityStore(activity_path).save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "DCE",
                    "M",
                    "20260823",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog_count=20,
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    assert not _checks(report)["directional_activity_ready"]


def test_doctor_rejects_missing_sampled_metadata(tmp_path: Path) -> None:
    config = _config(tmp_path)

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog_count=20,
        requested_symbols=["m2609"],
        metadata={},
        min_free_bytes=1,
    )

    assert not _checks(report)["live_metadata_complete"]
