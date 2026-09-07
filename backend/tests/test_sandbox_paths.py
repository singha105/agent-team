"""Sandbox escape attempts must be rejected.

Spec section 3 requires that every path resolve inside the workspace and that
`..` traversal and symlink escapes are blocked. These are the hostile cases.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agents.sandbox import (
    SandboxViolation,
    resolve_in_workspace,
    to_workspace_relative,
    workspace_root,
)

HOSTILE_PATHS = [
    pytest.param("../etc/passwd", id="parent-traversal"),
    pytest.param("../../../../../../etc/passwd", id="deep-traversal"),
    pytest.param("/etc/passwd", id="absolute-path"),
    pytest.param("/", id="absolute-root"),
    pytest.param("src/../../../etc/shadow", id="traversal-after-valid-prefix"),
    pytest.param("./../../root/.ssh/id_rsa", id="dot-slash-traversal"),
    pytest.param("a/b/c/../../../../outside.txt", id="traversal-past-root"),
    pytest.param("with\x00null", id="null-byte"),
    pytest.param("", id="empty-string"),
    pytest.param("   ", id="whitespace-only"),
]


@pytest.mark.parametrize("hostile", HOSTILE_PATHS)
def test_hostile_paths_are_rejected(hostile: str, settings) -> None:
    with pytest.raises(SandboxViolation):
        resolve_in_workspace(hostile, settings)


def test_symlink_to_directory_outside_workspace_is_rejected(workspace: Path, settings) -> None:
    """A symlink planted inside the workspace must not become an exit."""
    outside = workspace.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, workspace / "link")

    with pytest.raises(SandboxViolation):
        resolve_in_workspace("link/secret.txt", settings)


def test_symlink_to_file_outside_workspace_is_rejected(workspace: Path, settings) -> None:
    outside_file = workspace.parent / "secret.txt"
    outside_file.write_text("secret")
    os.symlink(outside_file, workspace / "innocent.txt")

    with pytest.raises(SandboxViolation):
        resolve_in_workspace("innocent.txt", settings)


def test_nested_symlink_chain_is_rejected(workspace: Path, settings) -> None:
    """Two hops still has to resolve to the same verdict."""
    outside = workspace.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, workspace / "hop1")
    os.symlink(workspace / "hop1", workspace / "hop2")

    with pytest.raises(SandboxViolation):
        resolve_in_workspace("hop2/secret.txt", settings)


def test_tilde_is_literal_not_home(settings) -> None:
    """'~' must be a directory name, never expanded to $HOME."""
    resolved = resolve_in_workspace("~/.ssh/id_rsa", settings)
    assert resolved.is_relative_to(workspace_root(settings))
    assert "~" in resolved.parts


@pytest.mark.parametrize(
    "legitimate",
    [".", "main.py", "api/main.py", "a/b/c/deep.txt", "./api/main.py", "dir/"],
)
def test_legitimate_paths_are_allowed(legitimate: str, settings) -> None:
    resolved = resolve_in_workspace(legitimate, settings)
    root = workspace_root(settings)
    assert resolved == root or resolved.is_relative_to(root)


def test_new_file_in_new_directory_resolves(settings) -> None:
    """Writing a file whose parent does not exist yet must be permitted."""
    resolved = resolve_in_workspace("brand/new/file.py", settings)
    assert not resolved.exists()
    assert resolved.is_relative_to(workspace_root(settings))


def test_workspace_relative_round_trip(settings) -> None:
    assert to_workspace_relative(resolve_in_workspace("a/b.py", settings), settings) == "a/b.py"
    assert to_workspace_relative(resolve_in_workspace(".", settings), settings) == "."
