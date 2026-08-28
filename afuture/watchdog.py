"""Pure local watchdog evaluation for the durable runtime heartbeat."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .alerts import AlertManager, FileAlertSink, WebhookAlertSink
from .heartbeat import HeartbeatIntegrityError, load_heartbeat


@dataclass(frozen=True)
class WatchdogCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class WatchdogResult:
    checks: list[WatchdogCheck] = field(default_factory=list)
    facts: dict[str, object] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.checks)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(WatchdogCheck(name, bool(passed), detail))

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checks": [asdict(item) for item in self.checks],
            "facts": dict(self.facts),
            "orders_sent": 0,
            "cancels_sent": 0,
        }


def _age(raw: object, *, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return float(raw)


def evaluate_heartbeat(
    heartbeat: Mapping[str, Any],
    *,
    now_utc: datetime,
    max_age_seconds: float,
    expected_deployment_digest: str,
    expected_risk_overlay_digest: str,
    process_run_clean: bool,
) -> WatchdogResult:
    """Evaluate already-verified local facts without any Broker or state mutation."""

    if now_utc.tzinfo is None:
        raise ValueError("watchdog clock must be timezone-aware")
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)):
        raise ValueError("watchdog max age must be numeric")
    if float(max_age_seconds) <= 0:
        raise ValueError("watchdog max age must be positive")
    maximum = float(max_age_seconds)
    result = WatchdogResult(
        facts={
            "process_uuid": heartbeat.get("process_uuid"),
            "heartbeat_utc": heartbeat.get("heartbeat_utc"),
            "runtime_mode": heartbeat.get("runtime_mode"),
            "ctp_trading_day": heartbeat.get("ctp_trading_day"),
        }
    )

    raw_timestamp = heartbeat.get("heartbeat_utc")
    try:
        timestamp = datetime.fromisoformat(str(raw_timestamp))
        if timestamp.tzinfo is None:
            raise ValueError("timezone is missing")
        heartbeat_age = max(
            0.0,
            (
                now_utc.astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)
            ).total_seconds(),
        )
    except (TypeError, ValueError):
        heartbeat_age = float("inf")
    result.add(
        "heartbeat_freshness",
        heartbeat_age <= maximum,
        f"heartbeat age={heartbeat_age:.3f}s max={maximum:.3f}s",
    )

    process_status = heartbeat.get("process_status")
    result.add(
        "process_status",
        process_status == "running",
        f"process_status={process_status!r}",
    )

    broker_state = str(heartbeat.get("broker_connection_state", ""))
    broker_healthy = broker_state.lower() in {"ready", "healthy", "connected"}
    result.add("broker_health", broker_healthy, f"broker_connection_state={broker_state!r}")

    for field_name, check_name in (
        ("last_account_snapshot_age_seconds", "account_snapshot_freshness"),
        ("last_complete_position_snapshot_age_seconds", "position_snapshot_freshness"),
        ("max_required_quote_age_seconds", "quote_freshness"),
    ):
        try:
            age = _age(heartbeat.get(field_name), name=field_name)
        except ValueError as exc:
            result.add(check_name, False, str(exc))
        else:
            result.add(check_name, age <= maximum, f"age={age:.3f}s max={maximum:.3f}s")

    raw_queue = heartbeat.get("critical_queue")
    queue: Mapping[str, Any] = raw_queue if isinstance(raw_queue, Mapping) else {}
    backlog = queue.get("critical_backlog", 0)
    streak = queue.get("critical_backlog_streak", 0)
    backlog_valid = (
        type(backlog) is int
        and type(streak) is int
        and int(backlog) >= 0
        and int(streak) >= 0
    )
    backlog_healthy = bool(backlog_valid and not (int(backlog) > 0 and int(streak) >= 3))
    result.add(
        "critical_backlog",
        backlog_healthy,
        f"critical_backlog={backlog!r} streak={streak!r}",
    )

    actual_deployment = heartbeat.get("deployment_identity_digest")
    result.add(
        "deployment_identity",
        actual_deployment == expected_deployment_digest,
        "heartbeat deployment identity matches local seal"
        if actual_deployment == expected_deployment_digest
        else "heartbeat deployment identity differs from local seal",
    )
    actual_risk = heartbeat.get("risk_overlay_digest")
    result.add(
        "risk_overlay_identity",
        actual_risk == expected_risk_overlay_digest,
        "heartbeat risk overlay matches local configuration"
        if actual_risk == expected_risk_overlay_digest
        else "heartbeat risk overlay differs from local configuration",
    )

    runtime_mode = str(heartbeat.get("runtime_mode", ""))
    kill_switch = heartbeat.get("kill_switch")
    last_error = str(heartbeat.get("last_error_category", ""))
    runtime_healthy = runtime_mode != "HALTED" and kill_switch is False
    result.add(
        "runtime_state",
        runtime_healthy,
        f"runtime_mode={runtime_mode!r} kill_switch={kill_switch!r} "
        f"last_error_category={last_error!r}",
    )
    result.add(
        "process_run",
        process_run_clean is True,
        "current heartbeat is bound to the active process-run receipt"
        if process_run_clean is True
        else "unclean restart fence or process-run identity mismatch is pending",
    )
    return result


def _alert_manager(config: object) -> AlertManager:
    sinks: list[object] = [FileAlertSink(str(getattr(config, "alert_path")))]
    webhook = str(getattr(config, "alert_webhook", ""))
    if webhook:
        sinks.append(WebhookAlertSink(webhook))
    return AlertManager(sinks)  # type: ignore[arg-type]


def run_watchdog_once(
    *,
    config: object,
    config_path: str | Path,
    heartbeat_path: str | Path,
    max_age_seconds: float,
) -> WatchdogResult:
    """Read only local evidence; this function deliberately never imports the CTP Broker."""

    manager = _alert_manager(config)
    runtime = Path(str(getattr(config, "state_path"))).resolve(strict=False).parent
    try:
        from .deployment_identity import DeploymentIdentityStore
        from .directional_stress90_state import Stress90PolicyStateStore
        from .process_run import ProcessRunStore
        from .state import StateStore
        from .stress90_risk_overlay import stress90_risk_overlay_digest

        heartbeat = load_heartbeat(heartbeat_path)
        deployment = DeploymentIdentityStore(runtime / "deployment_identity.json").load_required()
        expected_risk = stress90_risk_overlay_digest(
            getattr(config, "directional"),
            getattr(config, "risk"),
        )
        process_ok = False
        process_detail = ""
        try:
            process = ProcessRunStore(runtime / "process_run.json").load_required()
            process_ok = bool(
                not process.clean_shutdown
                and process.process_uuid == heartbeat.get("process_uuid")
                and process.phase != "restart_fenced"
                and process.deployment_digest == deployment.checksum
            )
            process_detail = f"phase={process.phase} process_uuid={process.process_uuid}"
        except Exception as exc:
            process_detail = str(exc)
        result = evaluate_heartbeat(
            heartbeat,
            now_utc=datetime.now(timezone.utc),
            max_age_seconds=max_age_seconds,
            expected_deployment_digest=deployment.checksum,
            expected_risk_overlay_digest=expected_risk,
            process_run_clean=process_ok,
        )
        result.facts.update(
            heartbeat_path=str(Path(heartbeat_path).resolve(strict=False)),
            deployment_sequence=deployment.sequence,
            deployment_checksum=deployment.checksum,
            process_run_detail=process_detail,
            config_path=str(Path(config_path).resolve(strict=False)),
        )
        try:
            generic = StateStore(str(getattr(config, "state_path"))).load_required_record()
            generic_ok = bool(
                heartbeat.get("generic_state_sequence") == generic.sequence
                and heartbeat.get("generic_state_checksum") == generic.checksum
            )
            result.add(
                "generic_state_identity",
                generic_ok,
                "heartbeat generic state identity matches current durable state"
                if generic_ok
                else "heartbeat generic state identity differs from current durable state",
            )
        except Exception as exc:
            result.add("generic_state_identity", False, str(exc))
        try:
            policy = Stress90PolicyStateStore(
                runtime / "stress90_policy_state.json"
            ).load_required_record()
            policy_ok = bool(
                heartbeat.get("policy_state_sequence") == policy.sequence
                and heartbeat.get("policy_state_checksum") == policy.checksum
            )
            result.add(
                "policy_state_identity",
                policy_ok,
                "heartbeat policy state identity matches current durable state"
                if policy_ok
                else "heartbeat policy state identity differs from current durable state",
            )
        except Exception as exc:
            result.add("policy_state_identity", False, str(exc))
    except (HeartbeatIntegrityError, OSError, RuntimeError, ValueError) as exc:
        result = WatchdogResult()
        result.add("watchdog_local_evidence", False, str(exc))
        result.facts.update(
            heartbeat_path=str(Path(heartbeat_path).resolve(strict=False)),
            config_path=str(Path(config_path).resolve(strict=False)),
        )
    if not result.passed:
        manager.critical(
            "afuture watchdog detected unsafe runtime health",
            {
                "failed_checks": [check.name for check in result.checks if not check.passed],
                "error_category": "watchdog_health",
            },
        )
    manager.close()
    return result
