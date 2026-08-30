from __future__ import annotations

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
