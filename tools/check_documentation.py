"""Offline consistency checks for repository Markdown authority and references."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import fields
from pathlib import Path

from afuture.auto import AutoConfig
from afuture.cli import build_parser
from afuture.directional import DirectionalConfig
from afuture.models import ContractInfo, ContractSpec, FeeSpec, PairConfig, Tick
from afuture.risk import RiskConfig

CURRENT_RESEARCH_BASELINE = "482455dc57bc6a134f45232e290b4a49c3f7073d"
REPOSITORY_PATH_PREFIXES = (
    ".github/",
    "afuture/",
    "config/",
    "docs/",
    "examples/",
    "tests/",
    "tools/",
)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
DOCUMENTED_COMMAND = re.compile(r"(?m)^\s*afuture\s+([a-z][a-z0-9-]*)\b")
ARCHIVE_NOTICE_MARKERS = (
    "> **归档说明：**",
    "> **Historical record.**",
    "> **Historical governance record.**",
    "> **Superseded checkpoint.**",
    "> **Superseded but reproducible checkpoint.**",
)
REQUIRED_AUTHORITY_DOCUMENTS = (
    "docs/configuration.md",
    "docs/data-formats.md",
    "docs/strategies.md",
    "docs/troubleshooting.md",
)
STATIC_CONFIGURATION_FIELDS = {
    "system": ("mode", "initial_capital"),
    "ctp": ("environment", "td_address", "md_address"),
    "execution": (
        "slippage_ticks",
        "aggressive_ticks",
        "auto_flatten_imbalance",
        "legging_timeout_seconds",
        "conservative_simulation",
        "latency_ticks",
        "market_impact_ticks",
        "require_live_metadata",
        "metadata_timeout_seconds",
    ),
    "paths": ("state", "log", "report", "journal", "alert"),
    "alert": ("webhook",),
}
CONFIGURATION_DATACLASSES = {
    "risk": RiskConfig,
    "auto": AutoConfig,
    "directional": DirectionalConfig,
    "pairs": PairConfig,
    "contracts": ContractInfo,
    "contract_spec": ContractSpec,
    "contracts.fee": FeeSpec,
}
CONFIGURATION_ENVIRONMENT_VARIABLES = (
    "AFUTURE_CTP_USER",
    "AFUTURE_CTP_PASSWORD",
    "AFUTURE_CTP_BROKER",
    "AFUTURE_CTP_APP_ID",
    "AFUTURE_CTP_AUTH_CODE",
    "AFUTURE_LIVE_ACK",
    "AFUTURE_RECOVERY_ACK",
)


def repository_markdown_files(root: Path) -> list[Path]:
    files = [path for path in root.glob("*.md") if path.is_file()]
    docs = root / "docs"
    if docs.exists():
        files.extend(path for path in docs.rglob("*.md") if path.is_file())
    return sorted(files)


def _local_link_target(markdown: Path, raw_target: str) -> Path | None:
    target = raw_target.strip().split(maxsplit=1)[0]
    if not target or target.startswith(("#", "http://", "https://", "mailto:")):
        return None
    target = target.split("#", 1)[0]
    if not target:
        return None
    return (markdown.parent / target).resolve()


def check_local_references(
    root: Path,
    markdown: Path,
    *,
    check_backticks: bool = True,
) -> list[str]:
    errors: list[str] = []
    text = markdown.read_text(encoding="utf-8")
    for raw_target in MARKDOWN_LINK.findall(text):
        target = _local_link_target(markdown, raw_target)
        if target is not None and not target.exists():
            errors.append(
                f"{markdown.relative_to(root)}: missing local Markdown link: {raw_target}"
            )

    if not check_backticks:
        return errors

    for raw_token in INLINE_CODE.findall(text):
        token = raw_token.strip().rstrip(".,:;")
        if not token.startswith(REPOSITORY_PATH_PREFIXES):
            continue
        if any(character in token for character in " *{}[]$<>"):
            continue
        target = (root / token.split("#", 1)[0]).resolve()
        if not target.exists():
            errors.append(
                f"{markdown.relative_to(root)}: missing backticked repository path: {token}"
            )
    return errors


def parser_subcommands() -> set[str]:
    parser = build_parser()
    for action in parser._actions:
        if action.dest == "command" and action.choices:
            return set(action.choices)
    raise RuntimeError("CLI parser has no command choices")


def documented_cli_subcommands(readme: Path) -> set[str]:
    return set(DOCUMENTED_COMMAND.findall(readme.read_text(encoding="utf-8")))


def check_archive_notices(root: Path) -> list[str]:
    """Prevent direct links to archived plans from looking like current authority."""
    archive = root / "docs" / "archive"
    if not archive.exists():
        return []
    errors: list[str] = []
    for path in sorted(archive.rglob("*.md")):
        head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:6])
        if not any(marker in head for marker in ARCHIVE_NOTICE_MARKERS):
            errors.append(f"{path.relative_to(root)}: missing top-level archive notice")
    return errors


def _documented_inline_tokens(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {token.strip() for token in INLINE_CODE.findall(path.read_text(encoding="utf-8"))}


def supported_configuration_fields() -> set[str]:
    result = {
        f"{section}.{name}"
        for section, names in STATIC_CONFIGURATION_FIELDS.items()
        for name in names
    }
    for section, model in CONFIGURATION_DATACLASSES.items():
        for item in fields(model):
            if section == "contract_spec":
                if item.name != "fee":
                    result.add(f"contracts.{item.name}")
            else:
                result.add(f"{section}.{item.name}")
    return result


def check_configuration_reference(root: Path) -> list[str]:
    reference = root / "docs" / "configuration.md"
    if not reference.exists():
        return ["docs/configuration.md: missing configuration reference"]
    documented = _documented_inline_tokens(reference)
    errors = [
        f"docs/configuration.md: undocumented configuration field: {name}"
        for name in sorted(supported_configuration_fields().difference(documented))
    ]
    errors.extend(
        f"docs/configuration.md: undocumented environment variable: {name}"
        for name in CONFIGURATION_ENVIRONMENT_VARIABLES
        if name not in documented
    )
    return errors


def check_tick_schema_reference(root: Path) -> list[str]:
    reference = root / "docs" / "data-formats.md"
    if not reference.exists():
        return ["docs/data-formats.md: missing Tick CSV schema reference"]
    documented = _documented_inline_tokens(reference)
    return [
        f"docs/data-formats.md: undocumented Tick CSV field: {item.name}"
        for item in fields(Tick)
        if item.name not in documented
    ]


def check_required_authority_documents(root: Path) -> list[str]:
    readme_tokens = (root / "README.md").read_text(encoding="utf-8")
    index_tokens = (root / "docs" / "documentation-index.md").read_text(encoding="utf-8")
    errors: list[str] = []
    for relative in REQUIRED_AUTHORITY_DOCUMENTS:
        path = root / relative
        if not path.exists():
            errors.append(f"{relative}: missing required current authority document")
            continue
        readme_target = relative
        index_target = Path(relative).name
        if readme_target not in readme_tokens:
            errors.append(f"README.md: missing current authority link: {relative}")
        if index_target not in index_tokens:
            errors.append(
                f"docs/documentation-index.md: missing current authority link: {relative}"
            )
    return errors


def _classified_markdown(root: Path, index: Path) -> Counter[Path]:
    result: Counter[Path] = Counter()
    text = index.read_text(encoding="utf-8")
    for raw_target in MARKDOWN_LINK.findall(text):
        target = _local_link_target(index, raw_target)
        if target is not None and target.suffix == ".md" and target.is_relative_to(root):
            result[target] += 1
    return result


def _historical_markdown(root: Path, index: Path) -> list[Path]:
    text = index.read_text(encoding="utf-8")
    match = re.search(
        r"## (?:Historical or superseded record|历史或已替代记录)\n"
        r"(?P<body>.*?)(?=\n## )",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        return []
    paths: list[Path] = []
    for raw_target in MARKDOWN_LINK.findall(match.group("body")):
        target = _local_link_target(index, raw_target)
        if target is not None and target.suffix == ".md" and target.is_relative_to(root):
            paths.append(target)
    return paths


def check_repository(root: Path) -> list[str]:
    root = root.resolve()
    markdown_files = repository_markdown_files(root)
    errors = [
        error
        for markdown in markdown_files
        for error in check_local_references(
            root,
            markdown,
            # Plans/specifications intentionally preserve proposed or later-deleted
            # paths. Operational authority and evidence must reference real paths.
            check_backticks=(
                "docs/archive/development/" not in markdown.relative_to(root).as_posix()
            ),
        )
    ]
    errors.extend(check_archive_notices(root))
    errors.extend(check_configuration_reference(root))
    errors.extend(check_tick_schema_reference(root))
    errors.extend(check_required_authority_documents(root))

    index = root / "docs" / "documentation-index.md"
    if not index.exists():
        errors.append("docs/documentation-index.md: missing documentation authority index")
        return sorted(errors)

    classified = _classified_markdown(root, index)
    expected = set(markdown_files)
    missing = sorted(expected.difference(classified))
    duplicates = sorted(path for path, count in classified.items() if count != 1)
    extra = sorted(set(classified).difference(expected))
    errors.extend(f"documentation index missing: {path.relative_to(root)}" for path in missing)
    errors.extend(
        f"documentation index classifies more than once: {path.relative_to(root)}"
        for path in duplicates
    )
    errors.extend(f"documentation index references unknown file: {path}" for path in extra)

    documented = documented_cli_subcommands(root / "README.md")
    commands = parser_subcommands()
    for command in sorted(commands.difference(documented)):
        errors.append(f"README.md: undocumented CLI subcommand: {command}")
    for command in sorted(documented.difference(commands)):
        errors.append(f"README.md: unknown documented CLI subcommand: {command}")

    for historical in _historical_markdown(root, index):
        if CURRENT_RESEARCH_BASELINE in historical.read_text(encoding="utf-8"):
            errors.append(
                f"{historical.relative_to(root)}: current baseline SHA appears in historical record"
            )
    return sorted(errors)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check_repository(root)
    for error in errors:
        print(error)
    if errors:
        return 1
    print(f"documentation consistent: {len(repository_markdown_files(root))} Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
