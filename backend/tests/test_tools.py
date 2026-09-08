"""Tool handlers: behaviour, error shapes, and registry wiring."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agents.tools import build_toolset, registered_names
from app.agents.tools.base import get_tool
from app.agents.tools.filesystem import MAX_READ_BYTES, list_files, read_file, write_file
from app.agents.tools.shell import run_command


def test_every_expected_tool_is_registered() -> None:
    assert registered_names() == [
        "append_project_context",
        "ask_agent",
        "list_files",
        "read_file",
        "run_command",
        "send_message",
        "write_file",
    ]


def test_only_collaboration_tools_need_context() -> None:
    """Filesystem tools stay context-free so they remain testable in isolation
    and safe to run concurrently."""
    needs = {n for n in registered_names() if get_tool(n).needs_context}
    assert needs == {"send_message", "ask_agent", "append_project_context"}


def test_api_schemas_are_well_formed() -> None:
    schemas, _ = build_toolset(["read_file", "write_file", "list_files", "run_command"])
    for schema in schemas:
        assert set(schema) == {"name", "description", "input_schema"}
        assert schema["description"].strip()
        assert schema["input_schema"]["type"] == "object"
        for prop in schema["input_schema"]["properties"].values():
            assert prop["description"].strip(), "every parameter needs a description"


def test_unknown_tool_lookup_raises() -> None:
    with pytest.raises(KeyError, match="unknown tool"):
        get_tool("delete_everything")


# -- read_file -------------------------------------------------------------


async def test_read_file_returns_contents(settings, workspace: Path) -> None:
    (workspace / "a.py").write_text("x = 1\n")
    outcome = await read_file("a.py")
    assert outcome.content == "x = 1\n"
    assert not outcome.is_error


async def test_read_file_missing_is_an_error(settings) -> None:
    outcome = await read_file("nope.py")
    assert outcome.is_error
    assert "No such file" in outcome.content


async def test_read_file_on_a_directory_points_at_list_files(settings, workspace: Path) -> None:
    (workspace / "pkg").mkdir()
    outcome = await read_file("pkg")
    assert outcome.is_error
    assert "list_files" in outcome.content


async def test_read_file_rejects_escape(settings) -> None:
    outcome = await read_file("../../../etc/passwd")
    assert outcome.is_error
    assert "Denied" in outcome.content


async def test_read_file_rejects_oversized_file(settings, workspace: Path) -> None:
    (workspace / "big.txt").write_text("x" * (MAX_READ_BYTES + 1))
    outcome = await read_file("big.txt")
    assert outcome.is_error
    assert "read limit" in outcome.content


async def test_read_file_rejects_binary(settings, workspace: Path) -> None:
    (workspace / "b.bin").write_bytes(b"\xff\xfe\x00\x01")
    outcome = await read_file("b.bin")
    assert outcome.is_error
    assert "not UTF-8" in outcome.content


# -- write_file ------------------------------------------------------------


async def test_write_file_creates_parent_directories(settings, workspace: Path) -> None:
    outcome = await write_file("deep/nested/mod.py", "y = 2\n")
    assert not outcome.is_error
    assert (workspace / "deep/nested/mod.py").read_text() == "y = 2\n"
    assert "Created" in outcome.content


async def test_write_file_reports_overwrite(settings, workspace: Path) -> None:
    (workspace / "a.py").write_text("old")
    outcome = await write_file("a.py", "new")
    assert "Overwrote" in outcome.content
    assert (workspace / "a.py").read_text() == "new"


async def test_write_file_rejects_escape(settings, workspace: Path) -> None:
    outcome = await write_file("../pwned.txt", "x")
    assert outcome.is_error
    assert not (workspace.parent / "pwned.txt").exists()


async def test_write_file_through_symlink_is_rejected(settings, workspace: Path) -> None:
    outside = workspace.parent / "outside"
    outside.mkdir()
    os.symlink(outside, workspace / "link")
    outcome = await write_file("link/pwned.txt", "x")
    assert outcome.is_error
    assert not (outside / "pwned.txt").exists()


# -- list_files ------------------------------------------------------------


async def test_list_files_is_recursive(settings, workspace: Path) -> None:
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "mod.py").write_text("x")
    (workspace / "top.py").write_text("y")
    outcome = await list_files(".")
    assert "pkg/mod.py" in outcome.content
    assert "top.py" in outcome.content


async def test_list_files_on_empty_workspace(settings) -> None:
    outcome = await list_files(".")
    assert "empty" in outcome.content
    assert not outcome.is_error


async def test_list_files_on_a_file_points_at_read_file(settings, workspace: Path) -> None:
    (workspace / "a.py").write_text("x")
    outcome = await list_files("a.py")
    assert outcome.is_error
    assert "read_file" in outcome.content


async def test_list_files_does_not_follow_escaping_symlinks(settings, workspace: Path) -> None:
    outside = workspace.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, workspace / "link")

    outcome = await list_files(".")
    assert "secret" not in outcome.content
    assert "not accessible" in outcome.content


# -- run_command -----------------------------------------------------------


async def test_run_command_reports_exit_code_and_output(settings, workspace: Path) -> None:
    (workspace / "hello.txt").write_text("hi")
    outcome = await run_command("ls")
    assert "exit code: 0" in outcome.content
    assert "hello.txt" in outcome.content
    assert not outcome.is_error


async def test_run_command_denial_is_an_error_outcome(settings) -> None:
    outcome = await run_command("rm -rf /")
    assert outcome.is_error
    assert "Denied" in outcome.content
    assert outcome.payload["denied"] is True


async def test_run_command_failure_is_not_an_error_outcome(settings) -> None:
    """A failing command is normal feedback the agent must be able to act on."""
    outcome = await run_command("cat missing.txt")
    assert not outcome.is_error
    assert "exit code:" in outcome.content
    assert outcome.payload["exit_code"] != 0


async def test_list_files_survives_a_broken_symlink(settings, workspace: Path) -> None:
    """Agents create dangling symlinks routinely — a git checkout, a partial
    write. list_files is the first tool the system prompt tells them to call,
    so one bad entry must not take down the whole listing."""
    os.symlink(workspace / "nonexistent_target", workspace / "dangling")
    (workspace / "real.py").write_text("x = 1")

    outcome = await list_files(".")

    assert not outcome.is_error
    assert "real.py" in outcome.content
    assert "broken symlink" in outcome.content


async def test_list_files_survives_a_file_deleted_mid_listing(
    settings, workspace: Path, monkeypatch
) -> None:
    """A file removed between rglob() and stat() must not crash the listing."""
    (workspace / "vanishing.py").write_text("x")
    (workspace / "stable.py").write_text("y")

    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "vanishing.py":
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    outcome = await list_files(".")

    assert not outcome.is_error
    assert "stable.py" in outcome.content
