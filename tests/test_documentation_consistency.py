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


def test_checker_rejects_archive_document_without_top_notice(tmp_path: Path):
    archive = tmp_path / "docs" / "archive" / "old-plan.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("# Old plan\n\nStill looks current.\n", encoding="utf-8")

    errors = checker.check_archive_notices(tmp_path)

    assert errors == ["docs/archive/old-plan.md: missing top-level archive notice"]


def test_checker_accepts_standard_archive_notice(tmp_path: Path):
    archive = tmp_path / "docs" / "archive" / "old-plan.md"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        "# Old plan\n\n> **归档说明：** 本文只保存历史决策，不描述当前系统。\n",
        encoding="utf-8",
    )

    assert checker.check_archive_notices(tmp_path) == []


def test_current_configuration_reference_covers_every_supported_field():
    assert checker.check_configuration_reference(ROOT) == []


def test_current_data_format_reference_covers_tick_csv_schema():
    assert checker.check_tick_schema_reference(ROOT) == []


def test_required_current_authority_documents_exist_and_are_indexed():
    assert checker.check_required_authority_documents(ROOT) == []


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


def test_stress90_wiring_documents_preserve_activation_and_history_boundaries():
    productionization_path = ROOT / "docs" / "stress90-live-productionization.md"
    runbook_path = ROOT / "docs" / "stress90-live-runbook.md"
    assert productionization_path.is_file()
    assert runbook_path.is_file()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    evidence = (ROOT / "docs" / "stress90-final-evidence.md").read_text(encoding="utf-8")
    productionization = productionization_path.read_text(encoding="utf-8")
    index = (ROOT / "docs" / "documentation-index.md").read_text(encoding="utf-8")

    assert 'directional.policy = "stress90"' in readme
    assert "前一阶段不能自动授权后一阶段" in readme
    assert "`production_wiring=false`" in evidence
    assert "112.100053% 不是未来收益承诺" in readme
    assert "selection bias" in readme
    assert "CTP raw 60m" in readme and "历史 vendor" in readme
    for stage in (
        "固定历史候选验证",
        "代码 wiring 和自动化验证",
        "多日 CTP Shadow",
        "测试柜台订单生命周期",
        "极小真实资金",
        "扩大风险的人工许可",
    ):
        assert stage in productionization
    assert "stress90-live-productionization.md" in index
    assert "stress90-live-runbook.md" in index


def test_stress90_runbook_keeps_order_incapable_steps_before_live():
    runbook = (ROOT / "docs" / "stress90-live-runbook.md").read_text(encoding="utf-8")

    ordered_markers = (
        "afuture stress90-bootstrap",
        "afuture status",
        "afuture stress90-activate",
        "afuture directional-ohlc-refresh",
        "afuture doctor",
        "afuture shadow",
        "afuture stress90-oi-compare",
        "afuture live",
    )
    offsets = tuple(runbook.index(marker) for marker in ordered_markers)
    assert offsets == tuple(sorted(offsets))
    assert "绝不自动采用 `.prev`" in runbook
    assert "AFUTURE_STRESS90_REBASE_ACK=RESET_STRESS90_ACCOUNT_PATH" in runbook
    shadow_cache = "--cache runtime/shadow/directional_ohlc_cache.json"
    assert shadow_cache in runbook
    assert runbook.index(shadow_cache) < runbook.index("afuture shadow")
