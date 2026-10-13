from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


_RUN_WITHOUT_FCNTL = """
import builtins
import runpy
import sys

original_import = builtins.__import__


def import_without_fcntl(name, *args, **kwargs):
    if name == "fcntl" or name.startswith("fcntl."):
        raise ModuleNotFoundError("No module named 'fcntl'", name="fcntl")
    return original_import(name, *args, **kwargs)


builtins.__import__ = import_without_fcntl
sys.modules.pop("fcntl", None)
sys.argv = ["afuture", *sys.argv[1:]]
runpy.run_module("afuture", run_name="__main__")
"""


def test_core_cli_help_and_validation_do_not_import_posix_locking() -> None:
    commands = (
        ("--help",),
        ("validate", "--config", "config/afuture.example.toml"),
    )

    for command in commands:
        completed = subprocess.run(
            [sys.executable, "-c", _RUN_WITHOUT_FCNTL, *command],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, (
            f"{' '.join(command)} imported POSIX-only locking:\n{completed.stderr}"
        )


def test_sample_replay_closes_positions_and_releases_margin(tmp_path: Path) -> None:
    config = tmp_path / "replay.toml"
    config.write_text(
        (REPOSITORY_ROOT / "config/afuture.example.toml")
        .read_text(encoding="utf-8")
        .replace("runtime/", f"{tmp_path.as_posix()}/runtime/"),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "afuture",
            "replay",
            "--config",
            str(config),
            "--data",
            str(REPOSITORY_ROOT / "examples/sample_ticks.csv"),
            "--output-dir",
            str(tmp_path / "new-replay"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads((tmp_path / "new-replay/report.json").read_text(encoding="utf-8"))
    assert report["performance"]["trade_count"] == 4
    assert report["positions"] == []
    assert report["account"]["margin"] == 0


def test_replay_validates_inputs_and_preserves_existing_runtime(tmp_path):
    from dataclasses import replace

    import pytest

    from afuture.config import load_config
    from afuture.replay import run_replay

    config = replace(load_config(REPOSITORY_ROOT / "config/afuture.example.toml"),
                     state_path=str(tmp_path / "state.json"),
                     report_path=str(tmp_path / "report.json"))
    current, previous = tmp_path / "state.json", tmp_path / "state.json.prev"
    current.write_bytes(b"original halted evidence")
    previous.write_bytes(b"previous evidence")
    with pytest.raises(ValueError, match="mode"):
        run_replay(replace(config, mode="live"), tmp_path / "absent.csv")
    with pytest.raises(FileNotFoundError):
        run_replay(config, tmp_path / "absent.csv", output_dir=tmp_path / "new")
    assert not (tmp_path / "new").exists()
    data = REPOSITORY_ROOT / "examples/sample_ticks.csv"
    accounts = [run_replay(config, data), run_replay(config, data)]
    assert [account.equity for account in accounts] == [500040, 500040]
    assert len(list((tmp_path / "replays").glob("*/report.json"))) == 2
    with pytest.raises(FileExistsError):
        run_replay(config, data, output_dir=tmp_path)
    assert current.read_bytes() == b"original halted evidence"
    assert previous.read_bytes() == b"previous evidence"
