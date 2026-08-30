from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
DOCUMENTS = REPOSITORY_ROOT / "docs"


def test_ci_keeps_a_minimum_python_windows_core_smoke_without_live_adapters() -> None:
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in ci
    quality_job = ci.split("  quality:\n", maxsplit=1)[1].split("\n  windows-smoke:\n", maxsplit=1)[
        0
    ]
    assert "runs-on: ubuntu-latest" in quality_job
    assert 'python-version: "3.10"' in quality_job
    assert "python -m ruff check ." in quality_job
    assert "python -m ruff format --check ." in quality_job
    assert "python -m mypy afuture" in quality_job

    windows_job = ci.split("  windows-smoke:\n", maxsplit=1)[1].split("\n  test:\n", maxsplit=1)[0]
    assert "name: windows-smoke" in windows_job
    assert "runs-on: windows-latest" in windows_job
    assert 'python-version: "3.10"' in windows_job
    assert 'python -m pip install -e ".[dev]" -c constraints/core-dev.txt' in windows_job
    assert "python -m pip check" in windows_job
    assert "python -m compileall -q afuture" in windows_job
    assert "python -m afuture --help" in windows_job
    assert "afuture validate --config config/afuture.example.toml" in windows_job
    assert (
        "tests/test_cli_portability.py::test_core_cli_help_and_validation_do_not_import_posix_locking"
        in windows_job
    )
    assert "tests/test_config.py::test_load_config_rejects_lossy_integer_values" in windows_job
    assert (
        "tests/test_config.py::test_stationarity_compatibility_defaults_and_explicit_pair_value"
        in windows_job
    )
    assert (
        "tests/test_directional_stress90_final.py::test_fixed_input_loader_rejects_bad_input_before_any_csv_parser"
        in windows_job
    )
    assert (
        "tests/test_directional_stress90_final.py::test_fixed_input_loader_verifies_every_file_before_parsing_verified_bytes"
        in windows_job
    )
    assert (
        "tests/test_execution_aligned_policy.py::test_intraday_proxy_rejects_each_invalid_active_market_cell"
        in windows_job
    )
    assert (
        "tests/test_execution_aligned_policy.py::test_intraday_proxy_ignores_each_invalid_zero_exposure_market_cell"
        in windows_job
    )
    assert (
        "tests/test_execution_aligned_policy.py::test_intraday_proxy_accepts_return_at_exact_absolute_limit"
        in windows_job
    )
    assert (
        "tests/test_portfolio_time_alignment.py::test_insufficient_common_timestamp_buckets_reject_additional_risk"
        in windows_job
    )
    assert (
        "tests/test_directional_attribution.py::test_production_attribution_adds_flat_pathwise_robustness_diagnostics"
        in windows_job
    )
    assert (
        "tests/test_directional_stress90_final.py::test_final_matrix_assembly_rejects_manifest_with_wrong_frozen_digest"
        in windows_job
    )
    assert "tests/test_workflow_contracts.py" in windows_job
    assert ".[live]" not in windows_job
    assert "constraints/live.txt" not in windows_job
    assert "vnpy" not in windows_job
    assert "continue-on-error" not in windows_job
    assert "|| true" not in windows_job

    test_job = ci.split("\n  test:\n", maxsplit=1)[1]
    assert "runs-on: ubuntu-latest" in test_job
    assert 'python-version: ["3.10", "3.13"]' in test_job
    assert "python -m pytest -q" in test_job
    assert "python -m compileall -q afuture" in test_job


def test_research_workflows_are_manual_historical_and_non_promotional() -> None:
    for path in sorted(WORKFLOWS.glob("research-*.yml")):
        workflow = path.read_text(encoding="utf-8")

        assert "  workflow_dispatch:" in workflow
        for automatic_trigger in ("  push:", "  pull_request:", "  schedule:", "  workflow_call:"):
            assert automatic_trigger not in workflow
        assert "permissions:\n  contents: read" in workflow
        assert "Research-only historical evidence" in workflow
        assert "not a forecast or promotional claim" in workflow


def test_governance_docs_keep_runtime_evidence_and_ci_boundaries_explicit() -> None:
    data_and_backtest = (DOCUMENTS / "data-and-backtest.md").read_text(encoding="utf-8")
    architecture = (DOCUMENTS / "architecture.md").read_text(encoding="utf-8")

    for phrase in (
        "活跃敞口",
        "超过 20%",
        "零敞口",
        "五个固定输入",
        "任何解析前",
        "input_manifest",
        "只读诊断",
        "pathwise proxy",
        "不影响 gate",
    ):
        assert phrase in data_and_backtest
    for phrase in (
        "UNKNOWN correlation 不是零相关",
        "已有 incumbent",
        "Windows smoke",
        "核心纯 Python",
        "目标机 CTP ABI",
        "#11/#21",
        "historical closed-without-merge",
        "分支留存",
        "不授权真钱",
    ):
        assert phrase in architecture
