"""Validate the local Markdown conventions used by this repository.

This intentionally small validator handles ATX headings, inline Markdown links, and fenced code
blocks. It does not attempt to parse every CommonMark construct or contact external URLs.
"""

from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\)")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
INLINE_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\([^)]+\)")


@dataclass(frozen=True, slots=True)
class ValidationError:
    path: Path
    line: int
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    markdown_files: int
    headings: int
    relative_links: int
    anchor_links: int
    missing_files: int
    missing_anchors: int
    duplicate_slugs: int
    toc_errors: int
    errors: tuple[ValidationError, ...]

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class ParsedMarkdown:
    headings: frozenset[str]
    heading_count: int
    duplicate_errors: tuple[ValidationError, ...]
    active_lines: tuple[tuple[int, str], ...]
    toc_lines: frozenset[int]


def github_slug(value: str) -> str:
    """Return the GitHub-style base slug needed by the repository's headings."""
    value = html.unescape(value)
    value = INLINE_LINK_PATTERN.sub(r"\1", value)
    value = HTML_TAG_PATTERN.sub("", value)
    value = value.replace("`", "").replace("*", "").replace("~", "")
    characters = (
        character
        for character in value.lower().strip()
        if character.isalnum() or character in {" ", "\t", "-", "_"}
    )
    return re.sub(r"\s+", "-", "".join(characters))


def _active_lines(path: Path) -> tuple[tuple[int, str], ...]:
    lines: list[tuple[int, str]] = []
    fence_marker: str | None = None
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in {"```", "~~~"}:
            if fence_marker is None:
                fence_marker = marker
            elif marker == fence_marker:
                fence_marker = None
            continue
        if fence_marker is None:
            lines.append((line_number, line))
    return tuple(lines)


def _parse_markdown(path: Path, root: Path) -> ParsedMarkdown:
    active_lines = _active_lines(path)
    headings: set[str] = set()
    base_counts: dict[str, int] = {}
    duplicate_errors: list[ValidationError] = []
    toc_lines: set[int] = set()
    in_toc = False
    heading_count = 0

    for line_number, line in active_lines:
        match = HEADING_PATTERN.match(line)
        if match is not None:
            level = len(match.group(1))
            title = match.group(2)
            base_slug = github_slug(title)
            occurrence = base_counts.get(base_slug, 0)
            actual_slug = base_slug if occurrence == 0 else f"{base_slug}-{occurrence}"
            base_counts[base_slug] = occurrence + 1
            headings.add(actual_slug)
            heading_count += 1

            if occurrence:
                duplicate_errors.append(
                    ValidationError(
                        path=path.relative_to(root),
                        line=line_number,
                        kind="duplicate-slug",
                        message=f"heading reuses base slug #{base_slug}",
                    )
                )

            if level == 2:
                in_toc = title.strip().lower() in {"contents", "table of contents"}
            continue

        if in_toc:
            toc_lines.add(line_number)

    return ParsedMarkdown(
        headings=frozenset(headings),
        heading_count=heading_count,
        duplicate_errors=tuple(duplicate_errors),
        active_lines=active_lines,
        toc_lines=frozenset(toc_lines),
    )


def _markdown_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                path
                for path in root.rglob("*.md")
                if not any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts)
            ),
            key=lambda path: path.as_posix().lower(),
        )
    )


def _is_external(target: str) -> bool:
    parsed = urlsplit(target)
    return bool(parsed.scheme or parsed.netloc) or target.startswith("/")


def validate_repository(root: Path) -> ValidationReport:
    root = root.resolve()
    markdown_files = _markdown_files(root)
    parsed_by_path = {path.resolve(): _parse_markdown(path, root) for path in markdown_files}
    errors: list[ValidationError] = []
    relative_links = 0
    anchor_links = 0
    missing_files = 0
    missing_anchors = 0
    toc_errors = 0

    for parsed in parsed_by_path.values():
        errors.extend(parsed.duplicate_errors)

    for source_path, parsed in parsed_by_path.items():
        for line_number, line in parsed.active_lines:
            for match in LINK_PATTERN.finditer(line):
                target = match.group(1)
                if _is_external(target):
                    continue

                relative_links += 1
                path_text, separator, fragment_text = target.partition("#")
                fragment = unquote(fragment_text).lower()
                if separator:
                    anchor_links += 1

                target_path = (
                    source_path
                    if not path_text
                    else (source_path.parent / unquote(path_text)).resolve()
                )
                relative_source = source_path.relative_to(root)

                if not target_path.is_file():
                    missing_files += 1
                    errors.append(
                        ValidationError(
                            path=relative_source,
                            line=line_number,
                            kind="missing-file",
                            message=f"linked file does not exist: {target}",
                        )
                    )
                    if line_number in parsed.toc_lines:
                        toc_errors += 1
                    continue

                if fragment and target_path.suffix.lower() == ".md":
                    target_document = parsed_by_path.get(target_path)
                    if target_document is None or fragment not in target_document.headings:
                        missing_anchors += 1
                        errors.append(
                            ValidationError(
                                path=relative_source,
                                line=line_number,
                                kind="missing-anchor",
                                message=f"linked anchor does not exist: {target}",
                            )
                        )
                        if line_number in parsed.toc_lines:
                            toc_errors += 1

    duplicate_slugs = sum(len(parsed.duplicate_errors) for parsed in parsed_by_path.values())
    headings = sum(parsed.heading_count for parsed in parsed_by_path.values())
    return ValidationReport(
        markdown_files=len(markdown_files),
        headings=headings,
        relative_links=relative_links,
        anchor_links=anchor_links,
        missing_files=missing_files,
        missing_anchors=missing_anchors,
        duplicate_slugs=duplicate_slugs,
        toc_errors=toc_errors,
        errors=tuple(errors),
    )


def _print_report(report: ValidationReport) -> None:
    for error in report.errors:
        print(f"{error.path.as_posix()}:{error.line}: {error.kind}: {error.message}")
    print(f"Markdown files checked: {report.markdown_files}")
    print(f"Headings checked: {report.headings}")
    print(f"Relative links checked: {report.relative_links}")
    print(f"Anchor links checked: {report.anchor_links}")
    print(f"Missing files: {report.missing_files}")
    print(f"Missing anchors: {report.missing_anchors}")
    print(f"Duplicate heading slugs: {report.duplicate_slugs}")
    print(f"TOC errors: {report.toc_errors}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate local Markdown links and headings.")
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root; defaults to the parent of scripts/.",
    )
    arguments = parser.parse_args()
    report = validate_repository(arguments.root)
    _print_report(report)
    return 0 if report.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
