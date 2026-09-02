from __future__ import annotations

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
