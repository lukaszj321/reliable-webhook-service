from pathlib import Path

from scripts.validate_markdown import github_slug, validate_repository


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_valid_relative_file_and_anchor_links(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "# Home\n\n[Guide](docs/guide.md#quick-start)\n")
    _write(tmp_path / "docs" / "guide.md", "# Guide\n\n## Quick start\n")

    report = validate_repository(tmp_path)

    assert report.is_valid
    assert report.relative_links == 1
    assert report.anchor_links == 1


def test_missing_file_is_reported(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "# Home\n\n[Missing](docs/missing.md)\n")

    report = validate_repository(tmp_path)

    assert report.missing_files == 1
    assert report.errors[0].kind == "missing-file"


def test_missing_anchor_is_reported(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "# Home\n\n[Missing](#absent)\n")

    report = validate_repository(tmp_path)

    assert report.missing_anchors == 1
    assert report.errors[0].kind == "missing-anchor"


def test_duplicate_heading_slug_is_reported(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "# Home\n\n## Result\n\n## Result\n")

    report = validate_repository(tmp_path)

    assert report.duplicate_slugs == 1
    assert report.errors[0].kind == "duplicate-slug"


def test_fenced_code_blocks_are_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path / "README.md",
        "# Home\n\n```markdown\n## Home\n[Missing](missing.md)\n```\n",
    )

    report = validate_repository(tmp_path)

    assert report.is_valid
    assert report.headings == 1
    assert report.relative_links == 0


def test_external_url_is_not_checked_as_local_file(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "# Home\n\n[Project](https://example.test/docs#start)\n")

    report = validate_repository(tmp_path)

    assert report.is_valid
    assert report.relative_links == 0


def test_slug_normalization_matches_repository_conventions() -> None:
    assert github_slug("Manual replay and retry-cycle budget") == (
        "manual-replay-and-retry-cycle-budget"
    )
    assert github_slug("`webhook_endpoints` schema") == "webhook_endpoints-schema"


def test_repository_documentation_passes() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    report = validate_repository(repository_root)

    assert report.is_valid, report.errors
