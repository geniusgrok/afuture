from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_documentation",
    ROOT / "tools" / "check_documentation.py",
)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def test_repository_documentation_is_consistent():
    assert checker.check_repository(ROOT) == []


@pytest.mark.parametrize(
    "content,expected",
    [
        ("[missing](docs/missing.md)", "missing local Markdown link"),
        ("`config/missing.toml`", "missing backticked repository path"),
    ],
)
def test_checker_reports_broken_local_references(tmp_path: Path, content: str, expected: str):
    (tmp_path / "README.md").write_text(content, encoding="utf-8")

    errors = checker.check_local_references(tmp_path, tmp_path / "README.md")

    assert any(expected in error for error in errors)


def test_every_cli_subcommand_is_documented():
    documented = checker.documented_cli_subcommands(ROOT / "README.md")
    assert checker.parser_subcommands() == documented
