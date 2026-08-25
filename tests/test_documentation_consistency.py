from __future__ import annotations

import importlib.util
import re
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


def test_checker_recognizes_chinese_historical_section(tmp_path: Path):
    historical = tmp_path / "history.md"
    historical.write_text("historical", encoding="utf-8")
    index = tmp_path / "index.md"
    index.write_text(
        "# 索引\n\n## 历史或已替代记录\n\n- [记录](history.md)\n\n## 开发计划\n",
        encoding="utf-8",
    )

    assert checker._historical_markdown(tmp_path, index) == [historical]


def test_readme_explains_stress_research_label_in_plain_language():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "“90”表示该轮研究预先设定的压力情景年化收益目标" in readme
    assert "不是 90 个基点成本、90% 保证金或实盘风险等级" in readme


def test_readme_does_not_lead_with_git_lineage_noise():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "当前工程基线继承" not in readme
    assert "PR #" not in readme
    assert not re.search(r"\b[0-9a-f]{40}\b", readme)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/architecture.md",
        "docs/data-and-backtest.md",
        "docs/live-trading.md",
        "docs/production-checklist.md",
    ],
)
def test_current_entry_documents_link_the_shared_glossary(path: str):
    content = (ROOT / path).read_text(encoding="utf-8")

    assert "glossary.md" in content
