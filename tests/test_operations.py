import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import afuture.operations as operations
from afuture.directional import DirectionalConfig
from afuture.directional_activity import (
    ContractActivity,
    DirectionalActivitySnapshot,
    DirectionalActivityStore,
)
from afuture.models import (
    AccountSnapshot,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    FeeSpec,
    RuntimeMode,
)
from afuture.operations import build_doctor_report, build_local_status
from afuture.risk import RiskConfig
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
        directional=DirectionalConfig(
            enabled=directional,
            products=("M",) if directional else (),
        ),
        risk=RiskConfig(max_margin_ratio=0.35, min_available_ratio=0.25),
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(500_000, 500_000, 440_000, 60_000, 0, 0, "20260825")


def _catalog() -> list[ContractInfo]:
    return [ContractInfo("m2609", "DCE", "M", "2026-12-31", "2026-01-01")]


def _checks(report) -> dict[str, bool]:
    return {item.name: item.passed for item in report.checks}


def _resign_activity(envelope: dict) -> None:
    unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope["checksum"] = sha256(encoded).hexdigest()


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


def test_local_status_reports_invalid_utf8_previous_as_warning(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.state_path)
    store.save(RuntimeState(trading_day="20260824"))
    store.save(RuntimeState(trading_day="20260825"))
    store.previous_path.write_bytes(b"\xff")

    report = build_local_status(config, min_free_bytes=1)

    assert report.passed
    assert report.facts["previous_state"]["valid"] is False
    assert "invalid state UTF-8" in report.warnings[0]


def test_local_status_rejects_runtime_target_or_parent_that_is_not_a_directory(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    Path(config.log_path).mkdir()

    report = build_local_status(config, min_free_bytes=1)

    assert not report.passed
    assert not _checks(report)["runtime_paths_writable"]


def test_local_status_rejects_dangling_runtime_symlink(tmp_path: Path) -> None:
    config = _config(tmp_path)
    Path(config.alert_path).symlink_to(tmp_path / "missing-alert-target")

    report = build_local_status(config, min_free_bytes=1)

    assert not report.passed
    assert "symlink is not allowed" in next(
        item.detail for item in report.checks if item.name == "runtime_paths_writable"
    )


def test_local_status_requires_searchable_ancestors_and_writable_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    report_path = Path(config.report_path)
    report_path.write_text("existing", encoding="utf-8")
    real_access = operations.os.access

    def selective_access(path, mode):
        candidate = Path(path)
        if candidate == tmp_path and mode == operations.os.W_OK | operations.os.X_OK:
            return False
        if candidate == report_path and mode == operations.os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(operations.os, "access", selective_access)

    report = build_local_status(config, min_free_bytes=1)

    detail = next(item.detail for item in report.checks if item.name == "runtime_paths_writable")
    assert not report.passed
    assert "ancestor is not writable/searchable" in detail
    assert "write target is not writable" in detail


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
        catalog=_catalog(),
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
        catalog=_catalog(),
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


def test_doctor_rejects_invalid_trading_day_and_breached_account_limits(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = AccountSnapshot(500_000, 500_000, 300_000, 200_000, 0, 0, "20260825")

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="bad-day",
        account=account,
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    checks = _checks(report)
    assert not checks["trading_day_consistent"]
    assert not checks["account_risk_limits"]


def test_doctor_uses_persisted_equity_anchors_for_loss_and_drawdown(tmp_path: Path) -> None:
    config = _config(tmp_path)
    StateStore(config.state_path).save(
        RuntimeState(
            trading_day="20260825",
            day_start_equity=550_000,
            equity_high_watermark=600_000,
            reconciled=True,
            metadata_verified=True,
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
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    check = next(item for item in report.checks if item.name == "account_risk_limits")
    assert not check.passed
    assert "daily loss" in check.detail
    assert "drawdown" in check.detail


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
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    assert not _checks(report)["directional_activity_ready"]


def test_doctor_requires_activity_and_catalog_coverage_for_configured_products(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, directional=True)
    activity_path = tmp_path / "directional_activity.json"
    DirectionalActivityStore(activity_path).save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "rb2610": ContractActivity(
                    "rb2610",
                    "SHFE",
                    "RB",
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )

    unrelated = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )
    assert not _checks(unrelated)["directional_activity_ready"]

    DirectionalActivityStore(activity_path).save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "DCE",
                    "M",
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )
    covered = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )
    assert _checks(covered)["directional_activity_ready"]

    DirectionalActivityStore(activity_path).save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "DCE",
                    "M",
                    "20260824",
                    10,
                    20,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )
    below_threshold = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )
    assert not _checks(below_threshold)["directional_activity_ready"]


def test_doctor_rejects_tampered_directional_activity_envelope(tmp_path: Path) -> None:
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
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )
    envelope = json.loads(activity_path.read_text(encoding="utf-8"))
    completed = envelope.get("completed", envelope)
    completed["contracts"]["m2609"]["open_interest"] = 99_999
    activity_path.write_text(json.dumps(envelope), encoding="utf-8")

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    check = next(item for item in report.checks if item.name == "directional_activity_ready")
    assert not check.passed
    assert "invalid directional activity evidence" in check.detail


def test_doctor_rejects_activity_identity_that_disagrees_with_catalog(tmp_path: Path) -> None:
    config = _config(tmp_path, directional=True)
    DirectionalActivityStore(tmp_path / "directional_activity.json").save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "SHFE",
                    "RB",
                    "20260824",
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
        catalog=_catalog(),
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
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )
    envelope = json.loads(activity_path.read_text(encoding="utf-8"))
    envelope["completed"]["contracts"]["m2609"]["trading_day"] = "20260823"
    _resign_activity(envelope)
    activity_path.write_text(json.dumps(envelope), encoding="utf-8")

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
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
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata={},
        min_free_bytes=1,
    )

    assert not _checks(report)["live_metadata_complete"]
