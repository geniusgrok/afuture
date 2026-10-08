from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_prepare_session_failure_replaces_stale_success_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from afuture import command_router

    report = tmp_path / "preflight.json"
    report.write_text('{"passed":true}', encoding="utf-8")

    def invalid_config(*_args, **_kwargs):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(command_router, "load_config", invalid_config)
    code = command_router._run_new(
        "prepare-session", ["--config", "missing", "--output", str(report)]
    )
    assert code == 3
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
    code = command_router._run_new(
        "prepare-session", ["--config", "missing", "--output", str(report)]
    )
    assert code == 3
    assert target.read_text(encoding="utf-8") == '{"passed":true}'
    assert "report_error" in json.loads(capsys.readouterr().out)
