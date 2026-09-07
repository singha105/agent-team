"""Command parsing, the allow-list, container flags, and the timeout."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.sandbox import (
    ALLOWED_EXECUTABLES,
    CONTAINER_WORKDIR,
    SandboxViolation,
    build_docker_argv,
    parse_command,
    run_command,
)
from tests.conftest import requires_docker

REFUSED_COMMANDS = [
    pytest.param("rm -rf /", id="not-on-allow-list"),
    pytest.param("curl https://evil.example.com", id="network-tool"),
    pytest.param("/usr/bin/python -c 1", id="absolute-executable-path"),
    pytest.param("../../usr/bin/python", id="relative-executable-path"),
    pytest.param("python -c 1 && cat /etc/passwd", id="command-chaining"),
    pytest.param("cat /etc/passwd | mail me", id="pipe"),
    pytest.param("python x > /etc/cron.d/evil", id="redirect"),
    pytest.param("echo `whoami`", id="backtick-substitution"),
    pytest.param("python -c $(cat /etc/passwd)", id="dollar-substitution"),
    pytest.param("ls\nrm -rf /", id="embedded-newline"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param("python 'unclosed", id="unparseable-quoting"),
]


@pytest.mark.parametrize("command", REFUSED_COMMANDS)
def test_refused_commands(command: str) -> None:
    with pytest.raises(SandboxViolation):
        parse_command(command)


@pytest.mark.parametrize("executable", sorted(ALLOWED_EXECUTABLES))
def test_every_allow_listed_executable_parses(executable: str) -> None:
    assert parse_command(f"{executable} --version")[0] == executable


def test_arguments_are_preserved_verbatim() -> None:
    assert parse_command("python -m pytest -q tests/") == ["python", "-m", "pytest", "-q", "tests/"]
    assert parse_command("git commit -m 'a message'") == ["git", "commit", "-m", "a message"]


def test_docker_argv_carries_every_isolation_flag(settings) -> None:
    argv = build_docker_argv(["python", "-V"], ".", "agentteam-test", settings)
    joined = " ".join(argv)

    assert argv[:2] == ["docker", "run"]
    assert "--rm" in argv
    assert "--network none" in joined, "network must be disabled by default"
    assert "--read-only" in argv
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert f"--memory {settings.sandbox_memory}" in joined
    assert f"--pids-limit {settings.sandbox_pids_limit}" in joined
    assert f"--workdir {CONTAINER_WORKDIR}" in joined
    assert argv[-2:] == ["python", "-V"], "the agent command must be last"


def test_docker_argv_mounts_only_the_workspace(settings) -> None:
    argv = build_docker_argv(["ls"], ".", "n", settings)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--volume"]
    assert mounts == [f"{settings.workspace_dir.resolve()}:{CONTAINER_WORKDIR}:rw"]


def test_docker_argv_opt_in_network(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_network", True)
    assert "--network bridge" in " ".join(build_docker_argv(["ls"], ".", "n", settings))


def test_docker_argv_scopes_workdir_to_subdirectory(settings) -> None:
    argv = build_docker_argv(["ls"], "api", "n", settings)
    assert f"--workdir {CONTAINER_WORKDIR}/api" in " ".join(argv)


async def test_run_command_rejects_cwd_outside_workspace(settings) -> None:
    with pytest.raises(SandboxViolation):
        await run_command("ls", cwd="../..", settings=settings)


async def test_run_command_rejects_missing_cwd(settings) -> None:
    with pytest.raises(SandboxViolation):
        await run_command("ls", cwd="does/not/exist", settings=settings)


async def test_subprocess_mode_runs_and_reports_exit_code(settings, workspace) -> None:
    (workspace / "hello.txt").write_text("hi")
    result = await run_command("ls", settings=settings)
    assert result.exit_code == 0
    assert "hello.txt" in result.stdout
    assert result.mode == "subprocess"


async def test_nonzero_exit_is_a_result_not_an_exception(settings) -> None:
    """The agent needs the error output to fix its own code."""
    result = await run_command("cat nonexistent.txt", settings=settings)
    assert result.exit_code != 0
    assert not result.timed_out


async def test_timeout_kills_the_process_group(settings) -> None:
    result = await run_command(
        "python3 -c \"__import__('time').sleep(30)\"", settings=settings, timeout=2
    )
    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.duration_ms < 15_000, "kill must not wait for the full sleep"


@requires_docker
@pytest.mark.integration
async def test_container_cannot_reach_the_network(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    result = await run_command(
        "python -c \"__import__('urllib.request').request.urlopen('http://1.1.1.1')\"",
        settings=settings,
        timeout=30,
    )
    assert result.exit_code != 0


@requires_docker
@pytest.mark.integration
async def test_container_root_filesystem_is_read_only(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    result = await run_command(
        "python -c \"open('/evil','w').write('x')\"", settings=settings, timeout=30
    )
    assert result.exit_code != 0
    assert "read-only" in result.stderr.lower()


@requires_docker
@pytest.mark.integration
async def test_container_cannot_read_a_host_file_outside_the_workspace(
    settings, monkeypatch, tmp_path: Path
) -> None:
    """Plant a real canary on the host and prove the container cannot read it.

    Pointing at a path that happens not to exist on the runner (a developer's
    ~/.zshrc, say) would pass vacuously on Linux CI — 'cat' fails either way.
    The file has to genuinely exist outside the workspace for the assertion to
    mean anything.
    """
    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    canary = tmp_path / "canary.txt"
    canary.write_text("CANARY-MUST-NOT-LEAK")
    assert canary.is_file()

    result = await run_command(f"cat {canary}", settings=settings, timeout=30)

    assert result.exit_code != 0
    assert "CANARY-MUST-NOT-LEAK" not in result.stdout


@requires_docker
@pytest.mark.integration
async def test_container_writes_land_in_the_host_workspace(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    result = await run_command(
        "python -c \"open('made_in_container.txt','w').write('ok')\"",
        settings=settings,
        timeout=30,
    )
    assert result.exit_code == 0, result.stderr
    assert (settings.workspace_dir / "made_in_container.txt").read_text() == "ok"


@requires_docker
@pytest.mark.integration
async def test_container_timeout_kills_the_container(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    result = await run_command(
        "python -c \"__import__('time').sleep(60)\"", settings=settings, timeout=5
    )
    assert result.timed_out is True
    assert result.duration_ms < 30_000


async def test_missing_host_executable_returns_127_not_an_exception(settings) -> None:
    """An allow-listed executable that isn't installed must surface as a result.

    'python' is allow-listed and present in the container image, but on many
    hosts only 'python3' exists. In subprocess mode that has to reach the agent
    as readable output, not an unhandled FileNotFoundError.
    """
    result = await run_command("npm --version", settings=settings)
    assert result.exit_code in (0, 127)
    if result.exit_code == 127:
        assert "command not found" in result.stderr


def test_mount_source_defaults_to_the_resolved_workspace(settings) -> None:
    assert settings.sandbox_mount_source == settings.workspace_dir.resolve()


def test_host_workspace_override_is_used_as_the_mount_source(settings, monkeypatch) -> None:
    """When AgentTeam runs in a container the --volume path must name a HOST
    path; the daemon resolves it, not this process."""
    monkeypatch.setattr(settings, "host_workspace_root", Path("/host/real/workspace"))
    argv = build_docker_argv(["ls"], ".", "n", settings)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--volume"]
    assert mounts == [f"/host/real/workspace:{CONTAINER_WORKDIR}:rw"]


def test_blank_host_workspace_env_var_is_treated_as_unset(monkeypatch, tmp_path) -> None:
    """`cp .env.example .env` must not break the sandbox mount.

    An empty AGENTTEAM_HOST_WORKSPACE_ROOT= would otherwise parse as Path('.')
    and the sandbox would bind-mount the literal path '.' instead of the
    workspace.
    """
    from app.core import config as config_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGENTTEAM_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("AGENTTEAM_HOST_WORKSPACE_ROOT", "")
    config_module.get_settings.cache_clear()
    try:
        fresh = config_module.get_settings()
        assert fresh.host_workspace_root is None
        assert fresh.sandbox_mount_source == workspace.resolve()
    finally:
        config_module.get_settings.cache_clear()
