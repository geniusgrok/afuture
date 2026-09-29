from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.risk import RiskConfig


def test_router_uses_the_neutral_internal_command_dispatcher() -> None:
    from afuture import cli, command_router

    assert hasattr(cli, "run_command")
    assert not hasattr(cli, "main")
    assert not hasattr(command_router, "_delegate")


def test_heartbeat_settings_are_derived_from_the_loaded_config_only(tmp_path: Path) -> None:
    from afuture.config import AppConfig
    from afuture.heartbeat_config import heartbeat_settings

    config = AppConfig(
        mode="replay",
        initial_capital=1.0,
        contracts={},
        pairs=[],
        risk=RiskConfig(),
        ctp=None,
        state_path=str(tmp_path / "state.json"),
        heartbeat_path=str(tmp_path / "heartbeat.json"),
        heartbeat_interval_seconds=9.0,
    )

    settings = heartbeat_settings(config)
    assert settings.path == tmp_path / "heartbeat.json"
    assert settings.interval_seconds == 9.0


def test_watchdog_uses_heartbeat_settings_from_the_same_loaded_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture import command_router

    config = SimpleNamespace(
        directional=SimpleNamespace(enabled=True, policy="stress90"),
        state_path=str(tmp_path / "state.json"),
        heartbeat_path=tmp_path / "current-heartbeat.json",
        heartbeat_interval_seconds=8.0,
    )
    observed: dict[str, object] = {}
    monkeypatch.setattr(command_router, "load_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(
        "afuture.watchdog.run_watchdog_once",
        lambda **kwargs: (
            observed.update(kwargs)
            or SimpleNamespace(passed=True, to_dict=lambda: {"passed": True})
        ),
    )

    assert command_router._run_new("watchdog", ["--config", "ignored", "--once"]) == 0
    assert observed["heartbeat_path"] == tmp_path / "current-heartbeat.json"
    assert observed["config"] is config


def test_prepare_session_failure_replaces_stale_success_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from afuture import command_router

    report = tmp_path / "preflight.json"
    report.write_text('{"passed":true}', encoding="utf-8")

    def invalid_config(*_args, **_kwargs):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(command_router, "load_config", invalid_config)
    assert command_router._run_new(
        "prepare-session", ["--config", "missing", "--output", str(report)]
    ) == 3
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["passed"] is False
    assert result["error"] == "config unavailable"
    assert result["orders_sent"] == result["cancels_sent"] == 0


def test_prepare_session_failure_does_not_follow_report_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from afuture import command_router

    target = tmp_path / "target.json"
    target.write_text('{"passed":true}', encoding="utf-8")
    report = tmp_path / "preflight.json"
    report.symlink_to(target)
    def invalid_config(*_args, **_kwargs):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(command_router, "load_config", invalid_config)
    assert command_router._run_new(
        "prepare-session", ["--config", "missing", "--output", str(report)]
    ) == 3
    assert target.read_text(encoding="utf-8") == '{"passed":true}'
    assert "report_error" in json.loads(capsys.readouterr().out)
