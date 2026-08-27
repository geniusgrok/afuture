from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from afuture.cli import build_parser  # noqa: E402
from afuture.command_router import _NEW_COMMANDS  # noqa: E402
from afuture.directional import DirectionalConfig  # noqa: E402
from afuture.execution_aligned_policy import (  # noqa: E402
    BASE_COST_BPS,
    EXECUTION_TEMPLATE_IDS,
    FROZEN_PRODUCTS,
    MAX_GROSS_LEVERAGE,
    META_COUNT,
    META_LOOKBACK,
    META_REBALANCE,
    STRESS_COST_BPS,
)
from afuture.risk import RiskConfig  # noqa: E402

CURRENT_DOCS = {
    Path("README.md"),
    Path("docs/architecture.md"),
    Path("docs/configuration.md"),
    Path("docs/data-and-backtest.md"),
    Path("docs/data-formats.md"),
    Path("docs/glossary.md"),
    Path("docs/live-trading.md"),
    Path("docs/production-checklist.md"),
    Path("docs/strategies.md"),
    Path("docs/stress90-bounded-research-evidence.md"),
    Path("docs/stress90-final-evidence.md"),
    Path("docs/stress90-live-productionization.md"),
    Path("docs/stress90-live-runbook.md"),
    Path("docs/troubleshooting.md"),
}

ALLOWED_MARKDOWN = CURRENT_DOCS | {
    Path("AGENTS.md"),
    Path("docs/documentation-index.md"),
    Path("docs/refactoring/architecture-audit-20260825.md"),
    Path("docs/refactoring/industrial-refactoring-report-20260825.md"),
    Path("docs/refactoring/solo-operations-hardening-report-20260825.md"),
    Path("docs/stress90-live-continuation-prompt-20260826.md"),
    Path("docs/stress90-live-handoff-20260826.md"),
} | set(Path("docs/archive").rglob("*.md")) | set(Path("docs/superpowers").rglob("*.md"))

LEGACY_PATH_PATTERNS = (
    re.compile(r"docs/archive/(?:development|evidence)/[A-Za-z0-9_./-]+\.md"),
    re.compile(r"docs/superpowers/(?:plans|specs)/[A-Za-z0-9_./-]+\.md"),
    re.compile(r"docs/refactoring/[A-Za-z0-9_./-]+\.md"),
    re.compile(r"docs/stress90-live-(?:continuation-prompt|handoff)-20260826\.md"),
)

EXPLICIT_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:v\d+(?:\.\d+){1,3}|version\s*\d+(?:\.\d+){1,3})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
PHASE_RE = re.compile(r"(?<![A-Za-z0-9])phase\s*[-_ ]?\d+(?![A-Za-z0-9])", re.IGNORECASE)
LEGACY_WORD_RE = re.compile(r"(?<![A-Za-z0-9])legacy(?![A-Za-z0-9])", re.IGNORECASE)
OLD_WORD_RE = re.compile(r"(?<![A-Za-z0-9])old(?![A-Za-z0-9])", re.IGNORECASE)
NEW_WORD_RE = re.compile(r"(?<![A-Za-z0-9])new(?![A-Za-z0-9])", re.IGNORECASE)
CLI_COMMAND_RE = re.compile(r"(?m)^afuture\s+([a-z0-9-]+)\b")
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+\.md(?:#[^)]+)?)\)")
VERSIONED_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+.*(?:v\d+(?:\.\d+)+|version\s*\d+(?:\.\d+)+).*$", re.I)
PHASE_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+.*phase\s*[-_ ]?\d+.*$", re.I)


def _read(path: Path) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _all_markdown() -> set[Path]:
    return {
        path.relative_to(ROOT)
        for path in ROOT.rglob("*.md")
        if ".git" not in path.parts and ".venv" not in path.parts
    }


def _is_legacy_target(value: str) -> bool:
    normalized = value.split("#", 1)[0].lstrip("./")
    return any(pattern.fullmatch(normalized) for pattern in LEGACY_PATH_PATTERNS)


def _resolved_link(source: Path, raw_link: str) -> Path | None:
    target = raw_link.split("#", 1)[0]
    if not target or "://" in target or target.startswith("mailto:"):
        return None
    resolved = (ROOT / source.parent / target).resolve()
    try:
        return resolved.relative_to(ROOT)
    except ValueError:
        return None


def _collect_cli_commands() -> set[str]:
    parser = build_parser()
    choices: set[str] = set()
    for action in parser._actions:
        action_choices = getattr(action, "choices", None)
        if isinstance(action_choices, dict):
            choices.update(str(key) for key in action_choices)
    return choices | set(_NEW_COMMANDS)


def _check_markdown_governance(errors: list[str]) -> None:
    actual = _all_markdown()
    unmanaged = sorted(actual - ALLOWED_MARKDOWN)
    if unmanaged:
        errors.append(
            "unmanaged Markdown files found: " + ", ".join(path.as_posix() for path in unmanaged)
        )
    missing = sorted(path for path in ALLOWED_MARKDOWN if not (ROOT / path).exists())
    if missing:
        errors.append(
            "document governance references missing files: "
            + ", ".join(path.as_posix() for path in missing)
        )


def _check_current_surface_tokens(errors: list[str]) -> None:
    for path in sorted(CURRENT_DOCS):
        text = _read(path)
        if VERSIONED_HEADING_RE.search(text):
            errors.append(f"{path}: versioned current heading is forbidden")
        if PHASE_HEADING_RE.search(text):
            errors.append(f"{path}: phase-number current heading is forbidden")
        for match in MARKDOWN_LINK_RE.finditer(text):
            raw_link = match.group(1)
            resolved = _resolved_link(path, raw_link)
            if resolved is None:
                continue
            if resolved in CURRENT_DOCS:
                continue
            if _is_legacy_target(resolved.as_posix()) or resolved in {
                Path("docs/documentation-index.md"),
                Path("AGENTS.md"),
            }:
                continue
            if resolved.suffix == ".md":
                errors.append(f"{path}: current doc links to unmanaged Markdown {resolved}")


def _check_readme_links(errors: list[str]) -> None:
    text = _read(Path("README.md"))
    for match in MARKDOWN_LINK_RE.finditer(text):
        raw_link = match.group(1)
        resolved = _resolved_link(Path("README.md"), raw_link)
        if resolved is None:
            continue
        if resolved in CURRENT_DOCS or resolved == Path("docs/documentation-index.md"):
            continue
        errors.append(f"README.md links directly to non-current Markdown: {raw_link}")


def _check_links_exist(errors: list[str]) -> None:
    for source in sorted(_all_markdown()):
        text = _read(source)
        for match in MARKDOWN_LINK_RE.finditer(text):
            resolved = _resolved_link(source, match.group(1))
            if resolved is not None and not (ROOT / resolved).exists():
                errors.append(f"{source}: broken Markdown link {match.group(1)}")


def _check_cli_documentation(errors: list[str]) -> None:
    commands = _collect_cli_commands()
    documented: set[str] = set()
    for path in CURRENT_DOCS:
        documented.update(CLI_COMMAND_RE.findall(_read(path)))
    unknown = sorted(documented - commands)
    if unknown:
        errors.append("documentation references unknown CLI commands: " + ", ".join(unknown))


def _check_current_constants(errors: list[str]) -> None:
    readme = _read(Path("README.md"))
    configuration = _read(Path("docs/configuration.md"))
    strategies = _read(Path("docs/strategies.md"))
    final_evidence = _read(Path("docs/stress90-final-evidence.md"))
    runbook = _read(Path("docs/stress90-live-runbook.md"))

    if f"{len(FROZEN_PRODUCTS)} 品种" not in readme:
        errors.append("README.md does not state the current directional product count")
    if f"{len(EXECUTION_TEMPLATE_IDS)}" not in readme:
        errors.append("README.md does not state the current execution template count")
    if str(META_LOOKBACK) not in configuration or str(META_REBALANCE) not in configuration:
        errors.append("docs/configuration.md is missing current meta schedule constants")
    if str(META_COUNT) not in configuration:
        errors.append("docs/configuration.md is missing current meta-count constant")
    if str(MAX_GROSS_LEVERAGE) not in readme:
        errors.append("README.md is missing current gross leverage limit")
    if str(BASE_COST_BPS) not in final_evidence or str(STRESS_COST_BPS) not in final_evidence:
        errors.append("Stress-90 final evidence is missing current cost assumptions")
    if "8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28" not in runbook:
        errors.append("Stress-90 runbook is missing the fixed candidate SHA")
    if "Stress-90" not in strategies or "stress90" not in configuration:
        errors.append("current strategy/configuration docs do not describe Stress-90")


def _check_risk_defaults(errors: list[str]) -> None:
    configuration = _read(Path("docs/configuration.md"))
    defaults = RiskConfig()
    expected = {
        "risk.max_margin_ratio": defaults.max_margin_ratio,
        "risk.max_daily_loss_ratio": defaults.max_daily_loss_ratio,
        "risk.max_total_drawdown_ratio": defaults.max_total_drawdown_ratio,
        "risk.max_open_pairs": defaults.max_open_pairs,
        "risk.max_contract_volume": defaults.max_contract_volume,
        "risk.max_quote_age_seconds": defaults.max_quote_age_seconds,
        "risk.max_leg_skew_seconds": defaults.max_leg_skew_seconds,
        "risk.expiry_blackout_days": defaults.expiry_blackout_days,
        "risk.min_available_ratio": defaults.min_available_ratio,
        "risk.margin_estimate_buffer": defaults.margin_estimate_buffer,
        "risk.max_orders_per_minute": defaults.max_orders_per_minute,
        "risk.min_depth_multiple": defaults.min_depth_multiple,
        "risk.max_bid_ask_ticks": defaults.max_bid_ask_ticks,
        "risk.limit_distance_ticks": defaults.limit_distance_ticks,
        "risk.risk_budget_ratio": defaults.risk_budget_ratio,
        "risk.risk_sigma_multiplier": defaults.risk_sigma_multiplier,
        "risk.open_cooldown_minutes": defaults.open_cooldown_minutes,
        "risk.close_blackout_minutes": defaults.close_blackout_minutes,
    }
    for name, value in expected.items():
        needle = f"`{name}`"
        if needle not in configuration or f"`{value}`" not in configuration:
            errors.append(f"docs/configuration.md is missing default {name}={value}")


def _check_directional_defaults(errors: list[str]) -> None:
    configuration = _read(Path("docs/configuration.md"))
    defaults = DirectionalConfig()
    expected = {
        "directional.max_gross_leverage": defaults.max_gross_leverage,
        "directional.min_days_to_expiry": defaults.min_days_to_expiry,
        "directional.min_volume": defaults.min_volume,
        "directional.min_open_interest": defaults.min_open_interest,
        "directional.max_contract_volume": defaults.max_contract_volume,
        "directional.rebalance_window": defaults.rebalance_window,
        "directional.signal_max_age_hours": defaults.signal_max_age_hours,
        "directional.account_exclusive": str(defaults.account_exclusive).lower(),
        "directional.account_continuity_mode": defaults.account_continuity_mode,
    }
    for name, value in expected.items():
        if f"`{name}`" not in configuration or f"`{value}`" not in configuration:
            errors.append(f"docs/configuration.md is missing default {name}={value}")


def _check_deprecated_terms(errors: list[str]) -> None:
    for path in sorted(CURRENT_DOCS):
        text = _read(path)
        if path.name in {"stress90-final-evidence.md", "stress90-bounded-research-evidence.md"}:
            continue
        for label, pattern in (
            ("explicit version", EXPLICIT_VERSION_RE),
            ("phase term", PHASE_RE),
            ("legacy term", LEGACY_WORD_RE),
            ("old term", OLD_WORD_RE),
            ("new term", NEW_WORD_RE),
        ):
            matches = list(pattern.finditer(text))
            if matches:
                sample = ", ".join(match.group(0) for match in matches[:5])
                errors.append(f"{path}: current-surface {label} found: {sample}")


def main() -> int:
    errors: list[str] = []
    _check_markdown_governance(errors)
    _check_current_surface_tokens(errors)
    _check_readme_links(errors)
    _check_links_exist(errors)
    _check_cli_documentation(errors)
    _check_current_constants(errors)
    _check_risk_defaults(errors)
    _check_directional_defaults(errors)
    _check_deprecated_terms(errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Documentation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
