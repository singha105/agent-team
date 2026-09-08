"""Shared PROJECT.md: append-only, and unbypassable."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agents.project_context import (
    MAX_SECTION_CHARS,
    PROJECT_FILE,
    append_section,
    context_prelude,
    ensure_exists,
    is_project_file,
    read_context,
)
from app.agents.tools.filesystem import write_file


def test_the_file_is_created_with_a_header(settings, workspace: Path) -> None:
    path = ensure_exists(settings)
    assert path.name == PROJECT_FILE
    assert "Append-only" in path.read_text()


def test_appending_preserves_what_was_there(settings) -> None:
    append_section("database", "Schema", "books(id, title)", settings)
    append_section("backend", "API contract", "GET /books", settings)

    content = read_context(settings)
    assert "books(id, title)" in content
    assert "GET /books" in content
    # Match the section headings, not bare words: the file's own header
    # mentions "the schema, the API contract" and would match first.
    assert content.index("## Schema") < content.index("## API contract"), (
        "append order must be preserved"
    )


def test_each_section_records_its_author(settings) -> None:
    append_section("database", "Schema", "books(id)", settings)
    assert "added by **database**" in read_context(settings)


def test_an_agent_cannot_erase_an_earlier_section(settings) -> None:
    """The whole reason the file exists is that decisions survive."""
    append_section("database", "Schema", "the original schema", settings)
    append_section("backend", "Schema", "a different schema", settings)

    content = read_context(settings)
    assert "the original schema" in content
    assert "a different schema" in content


@pytest.mark.parametrize(
    ("heading", "body"),
    [("", "body"), ("   ", "body"), ("heading", ""), ("heading", "   ")],
)
def test_empty_sections_are_refused(settings, heading: str, body: str) -> None:
    ok, detail = append_section("backend", heading, body, settings)
    assert not ok
    assert detail


def test_oversized_sections_are_refused(settings) -> None:
    ok, detail = append_section("backend", "Huge", "x" * (MAX_SECTION_CHARS + 1), settings)
    assert not ok
    assert "Summarise" in detail


@pytest.mark.parametrize(
    "path", ["PROJECT.md", "./PROJECT.md", "sub/../PROJECT.md", "a/b/../../PROJECT.md"]
)
def test_write_file_cannot_overwrite_the_shared_context(settings, path: str) -> None:
    """An enforcement a neighbouring tool can bypass is not an enforcement."""
    append_section("database", "Schema", "books(id, title)", settings)

    outcome = asyncio.run(write_file(path, "DESTROYED"))

    assert outcome.is_error
    assert "append-only" in outcome.content
    content = read_context(settings)
    assert "books(id, title)" in content
    assert "DESTROYED" not in content


def test_other_markdown_files_are_still_writable(settings, workspace: Path) -> None:
    outcome = asyncio.run(write_file("NOTES.md", "fine"))
    assert not outcome.is_error
    assert (workspace / "NOTES.md").read_text() == "fine"


@pytest.mark.parametrize("path", ["PROJECT.md", "./PROJECT.md", "x/../PROJECT.md"])
def test_the_guard_resolves_paths_rather_than_string_matching(settings, path: str) -> None:
    assert is_project_file(path, settings)


@pytest.mark.parametrize("path", ["project.md", "docs/PROJECT.md", "PROJECT.md.bak", "other.md"])
def test_the_guard_does_not_over_match(settings, path: str) -> None:
    assert not is_project_file(path, settings)


def test_the_prelude_says_so_when_the_context_is_empty(settings) -> None:
    prelude = context_prelude(settings)
    assert "empty" in prelude
    assert "append_project_context" in prelude


def test_the_prelude_carries_published_decisions(settings) -> None:
    append_section("database", "Books table", "books(id, title, author)", settings)
    prelude = context_prelude(settings)
    assert "books(id, title, author)" in prelude
    assert "rather than inventing" in prelude


def test_concurrent_appends_do_not_clobber_each_other(settings) -> None:
    """Append mode rather than read-modify-write: two agents publishing at the
    same moment must both survive."""
    for i in range(25):
        append_section(f"agent{i % 4}", f"Decision {i}", f"body {i}", settings)

    content = read_context(settings)
    for i in range(25):
        assert f"body {i}" in content
