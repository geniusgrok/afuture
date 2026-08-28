from __future__ import annotations

from pathlib import Path

SYSTEMD = Path(__file__).resolve().parents[1] / "deploy" / "systemd"


def _read(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


def test_live_service_has_required_process_supervision_fields() -> None:
    text = _read("afuture-live.service")
    for required in (
        "Type=simple",
        "WorkingDirectory=",
        "EnvironmentFile=",
        "UMask=0077",
        "KillSignal=SIGTERM",
        "TimeoutStopSec=",
        "Restart=on-failure",
        "RestartPreventExitStatus=75",
        "StartLimitIntervalSec=",
        "StartLimitBurst=",
        "NoNewPrivileges=true",
        "LimitNOFILE=",
    ):
        assert required in text
    assert "PrivateTmp=true" not in text


def test_watchdog_timer_is_short_bounded_once_runner() -> None:
    service = _read("afuture-watchdog.service")
    timer = _read("afuture-watchdog.timer")
    assert "afuture watchdog" in service
    assert "--once" in service
    assert "OnUnitActiveSec=" in timer
    assert "Restart=" not in service
    for forbidden in (
        "confirm-live",
        "issue-stress90",
        "stress90-operator-roll-forward",
        "stress90-account-rebase",
    ):
        assert forbidden not in service


def test_systemd_assets_contain_no_credentials_or_activation_acks() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SYSTEMD.iterdir())
    )
    for forbidden in (
        "AFUTURE_CTP_USER=",
        "AFUTURE_CTP_PASSWORD=",
        "AFUTURE_CTP_BROKER=",
        "AFUTURE_CTP_ACCOUNT_ID=",
        "AuthCode",
        "AFUTURE_LIVE_ACK=",
        "AFUTURE_STRESS90_ACTIVATION_PERMIT_ACK=",
        "I_UNDERSTAND_FUTURES_RISK",
        "I_CONFIRM_STRESS90_TECHNICAL_ACTIVATION",
        "PrivateTmp=true",
        "reset --hard",
        "git clean",
        "rm -rf",
    ):
        assert forbidden not in combined


def test_live_service_does_not_automate_operator_or_permit_actions() -> None:
    text = _read("afuture-live.service")
    for forbidden in (
        "stress90-operator-roll-forward",
        "stress90-account-rebase",
        "--issue-stress90-permit",
        "stress90-activate",
        "deployment-seal",
    ):
        assert forbidden not in text


def test_watchdog_service_has_no_trading_control_commands() -> None:
    text = _read("afuture-watchdog.service")
    for forbidden in (
        "afuture live",
        "cancel",
        "send_order",
        "kill ",
        "systemctl restart",
        "stress90-operator-roll-forward",
        "stress90-account-rebase",
        "issue-stress90",
    ):
        assert forbidden not in text


def test_live_service_fence_exit_prevents_restart_loop() -> None:
    text = _read("afuture-live.service")
    assert "Restart=on-failure" in text
    assert "RestartPreventExitStatus=75" in text
    assert "StartLimitBurst=" in text


def test_systemd_assets_use_placeholders_not_real_identity() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SYSTEMD.iterdir())
    )
    assert "<AFUTURE_WORKDIR>" in combined
    assert "<AFUTURE_ENV_FILE>" in combined
    assert "ychenracing" not in combined
    assert "AccountID" not in combined


def test_optional_backup_timer_never_forces_live_stop() -> None:
    service = SYSTEMD / "afuture-backup.service"
    if not service.exists():
        return
    text = service.read_text(encoding="utf-8")
    assert "backup-runtime" in text
    for forbidden in ("systemctl stop", "systemctl kill", "rm ", "delete"):
        assert forbidden not in text
