"""Sandboxed filesystem and command execution.

Two ruff rules are suppressed inline below with reasons: ASYNC109 (the timeout
parameter cannot be replaced by asyncio.timeout, which would cancel the await
while leaving the process or container running) and S108 (the /tmp reference is
a tmpfs mount target inside the container, not a host path).

The boundary is enforced by the runtime, never by the system prompt. Two
mechanisms, layered:

  1. Path resolution (always). Every path an agent supplies is resolved —
     following symlinks — and rejected unless the result is inside the
     workspace root. This blocks `..` traversal, absolute paths, and symlinks
     planted inside the workspace that point out of it.

  2. Process isolation (mode-dependent). In `docker` mode each command runs in
     a disposable container with no network, a read-only root filesystem,
     dropped capabilities, and the workspace as the only writable mount. In
     `subprocess` mode commands run on the host with only the allow-list and
     the timeout.

Why the container matters: the allow-list includes `python`, `pip`, `npm` and
`git`, and each of those independently grants arbitrary code execution —
`python -c "import os; os.system(...)"` escapes in one command, `pip` and `npm`
run code at install time, and `git` aliases of the form `!sh -c '...'` run
arbitrary shell. The allow-list alone is not a security boundary. It is kept as
defense in depth and as a guard against accidents, not as the boundary.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

# Executables an agent may invoke. See the module docstring: this is defense in
# depth, not the security boundary.
ALLOWED_EXECUTABLES = frozenset(
    {"python", "python3", "pip", "pip3", "node", "npm", "pytest", "ls", "cat", "mkdir", "git"}
)

# Shell metacharacters are refused rather than interpreted. Commands are never
# run through a shell, so these would be passed as literal argv entries and
# confuse the agent; refusing with a clear message is better than silently
# treating `&&` as a filename.
SHELL_METACHARACTERS = ("|", "&", ";", ">", "<", "`", "$(", "\n", "\r")

CONTAINER_WORKDIR = "/workspace"


class SandboxViolation(PermissionError):
    """Raised when an agent attempts an operation outside the sandbox."""


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one sandboxed command."""

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    mode: str

    def as_dict(self) -> dict:
        return {
            "argv": self.argv,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "mode": self.mode,
        }


# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------


def workspace_root(settings: Settings | None = None) -> Path:
    """The resolved sandbox root. Created if missing."""
    s = settings or get_settings()
    root = s.workspace_dir
    root.mkdir(parents=True, exist_ok=True)
    # Resolve after mkdir so symlinked roots (e.g. /tmp on macOS) normalise.
    return root.resolve()


def resolve_in_workspace(rel_path: str, settings: Settings | None = None) -> Path:
    """Resolve an agent-supplied path, or raise SandboxViolation.

    The returned path is guaranteed to be inside the workspace root after
    symlink resolution. It may not exist yet — callers writing new files rely
    on that.
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise SandboxViolation("path must be a non-empty string")

    if "\x00" in rel_path:
        raise SandboxViolation("path contains a null byte")

    candidate = Path(rel_path)
    if candidate.is_absolute():
        raise SandboxViolation(
            f"absolute paths are not permitted: {rel_path!r}. "
            "Use a path relative to the workspace root."
        )

    # Deliberately no expanduser(): '~' is treated as a literal directory name,
    # so '~/.ssh/id_rsa' resolves inside the workspace rather than to $HOME.
    root = workspace_root(settings)
    resolved = (root / candidate).resolve()

    if resolved != root and not resolved.is_relative_to(root):
        raise SandboxViolation(
            f"path escapes the workspace: {rel_path!r} resolves outside the sandbox root. "
            "All file access is confined to the workspace."
        )
    return resolved


def to_workspace_relative(path: Path, settings: Settings | None = None) -> str:
    """Render an absolute in-workspace path back as an agent-facing relative one."""
    root = workspace_root(settings)
    return "." if path == root else str(path.relative_to(root))


# --------------------------------------------------------------------------
# Command parsing
# --------------------------------------------------------------------------


def parse_command(command: str) -> list[str]:
    """Split a command into argv and check it against the allow-list."""
    if not isinstance(command, str) or not command.strip():
        raise SandboxViolation("command must be a non-empty string")

    for meta in SHELL_METACHARACTERS:
        if meta in command:
            raise SandboxViolation(
                f"command contains the shell metacharacter {meta!r}. Commands are not run "
                "through a shell — pipes, redirects and chaining are unavailable. "
                "Run one program at a time."
            )

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise SandboxViolation(f"could not parse command: {exc}") from exc

    if not argv:
        raise SandboxViolation("command is empty after parsing")

    executable = Path(argv[0]).name
    if executable != argv[0]:
        raise SandboxViolation(
            f"command must be a bare executable name, not a path: {argv[0]!r}. "
            f"Allowed: {sorted(ALLOWED_EXECUTABLES)}"
        )
    if executable not in ALLOWED_EXECUTABLES:
        raise SandboxViolation(
            f"executable {executable!r} is not allowed. " f"Allowed: {sorted(ALLOWED_EXECUTABLES)}"
        )
    return argv


def _truncate(raw: bytes, limit: int) -> tuple[str, bool]:
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text, False
    keep = limit // 2
    omitted = len(text) - 2 * keep
    return (
        f"{text[:keep]}\n\n... [{omitted} characters omitted] ...\n\n{text[-keep:]}",
        True,
    )


# --------------------------------------------------------------------------
# Executors
# --------------------------------------------------------------------------


async def _run_subprocess(
    argv: list[str], cwd: Path, timeout: int  # noqa: ASYNC109
) -> tuple[int, bytes, bytes, bool]:
    """Run on the host. start_new_session makes the child a process-group
    leader so the whole tree can be killed on timeout, not just the parent."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        # The executable is allow-listed but not installed on this host. 127 is
        # the conventional "command not found" code; returning it as a result
        # rather than raising lets the agent read the message and adapt.
        return (
            127,
            b"",
            (
                f"{argv[0]}: command not found on this host. Sandbox mode is "
                f"'subprocess', so only executables installed on the host are "
                f"available."
            ).encode(),
            False,
        )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout, stderr, False
    except TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # pragma: no cover - race
            pass
        with suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
        return 124, b"", f"command exceeded the {timeout}s timeout and was killed".encode(), True


def build_docker_argv(
    argv: list[str], rel_cwd: str, container_name: str, settings: Settings
) -> list[str]:
    """Construct the full `docker run` command line.

    Split out from execution so the exact flags can be asserted in tests
    without a Docker daemon, and logged verbatim to the trace.
    """
    workdir = CONTAINER_WORKDIR if rel_cwd == "." else f"{CONTAINER_WORKDIR}/{rel_cwd}"
    docker_argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        "bridge" if settings.sandbox_network else "none",
        "--read-only",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--memory",
        settings.sandbox_memory,
        "--memory-swap",
        settings.sandbox_memory,
        "--pids-limit",
        str(settings.sandbox_pids_limit),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",  # noqa: S108 - in-container mount target
        "--volume",
        f"{settings.sandbox_mount_source}:{CONTAINER_WORKDIR}:rw",
        "--workdir",
        workdir,
        "--env",
        "HOME=/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        settings.sandbox_image,
    ]
    return docker_argv + argv


async def _run_docker(
    argv: list[str], rel_cwd: str, timeout: int, settings: Settings  # noqa: ASYNC109
) -> tuple[int, bytes, bytes, bool]:
    """Run inside a disposable container."""
    container_name = f"agentteam-{uuid.uuid4().hex[:12]}"
    docker_argv = build_docker_argv(argv, rel_cwd, container_name, settings)

    proc = await asyncio.create_subprocess_exec(
        *docker_argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout, stderr, False
    except TimeoutError:
        # Kill the container, not just the local `docker run` client — killing
        # the client alone would leave the workload running in the daemon.
        killer = await asyncio.create_subprocess_exec(
            "docker",
            "kill",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        with suppress(Exception):
            await asyncio.wait_for(killer.wait(), timeout=10)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # pragma: no cover - race
            pass
        with suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
        return (
            124,
            b"",
            f"command exceeded the {timeout}s timeout; container was killed".encode(),
            True,
        )


async def run_command(
    command: str,
    cwd: str = ".",
    settings: Settings | None = None,
    timeout: int | None = None,  # noqa: ASYNC109
) -> CommandResult:
    """Execute one command in the sandbox.

    Raises SandboxViolation before anything runs if the command or cwd is not
    permitted. A non-zero exit code is a normal result, not an exception —
    the agent needs to see the error output to fix its own code.
    """
    s = settings or get_settings()
    argv = parse_command(command)
    resolved_cwd = resolve_in_workspace(cwd, s)

    if not resolved_cwd.is_dir():
        raise SandboxViolation(f"working directory does not exist in the workspace: {cwd!r}")

    limit = timeout or s.command_timeout_seconds
    rel_cwd = to_workspace_relative(resolved_cwd, s)

    started = asyncio.get_running_loop().time()
    if s.sandbox_mode == "docker":
        code, out, err, timed_out = await _run_docker(argv, rel_cwd, limit, s)
    else:
        code, out, err, timed_out = await _run_subprocess(argv, resolved_cwd, limit)
    duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)

    stdout, t1 = _truncate(out, s.max_output_bytes)
    stderr, t2 = _truncate(err, s.max_output_bytes)

    log.info(
        "command %s exit=%s %sms mode=%s%s",
        shlex.join(argv),
        code,
        duration_ms,
        s.sandbox_mode,
        " (timed out)" if timed_out else "",
    )
    return CommandResult(
        argv=argv,
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=timed_out,
        truncated=t1 or t2,
        mode=s.sandbox_mode,
    )
