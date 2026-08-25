"""Offline consistency checks for repository Markdown authority and references."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from afuture.cli import build_parser

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
