"""The run_command tool.

Command execution is delegated to app.agents.sandbox, which owns the security
boundary. This module only shapes the result for the model.
"""

from __future__ import annotations

from app.agents.sandbox import ALLOWED_EXECUTABLES, SandboxViolation
from app.agents.sandbox import run_command as sandbox_run
from app.agents.tools.base import Tool, ToolOutcome, register


def _format(result) -> str:
    """Render a command result for the model.

    stdout and stderr are labelled and the exit code is always stated — an
    agent that cannot tell success from failure will confidently build on a
    broken step.
    """
    parts = [f"exit code: {result.exit_code}"]
    if result.timed_out:
        parts.append("TIMED OUT — the command was killed.")
    if result.stdout.strip():
        parts.append(f"--- stdout ---\n{result.stdout.rstrip()}")
    if result.stderr.strip():
        parts.append(f"--- stderr ---\n{result.stderr.rstrip()}")
    if not result.stdout.strip() and not result.stderr.strip():
        parts.append("(no output)")
    return "\n".join(parts)


async def run_command(command: str, cwd: str = ".") -> ToolOutcome:
    try:
        result = await sandbox_run(command, cwd=cwd)
    except SandboxViolation as exc:
        return ToolOutcome(
            content=f"Denied: {exc}",
            payload={"command": command, "cwd": cwd, "denied": True},
            is_error=True,
        )
    except FileNotFoundError as exc:
        # Raised when the executor itself is missing — e.g. docker not installed.
        return ToolOutcome(
            content=(
                f"The sandbox could not start: {exc}. "
                "This is an environment problem, not something your code caused."
            ),
            payload={"command": command, "cwd": cwd, "executor_missing": True},
            is_error=True,
        )

    # A non-zero exit is reported as a normal result, not an error block: the
    # agent needs the output to diagnose and fix its own code.
    return ToolOutcome(
        content=_format(result),
        payload=result.as_dict() | {"cwd": cwd},
        is_error=False,
    )


register(
    Tool(
        name="run_command",
        description=(
            "Run a single command inside the sandboxed workspace and return its exit "
            "code, stdout and stderr. Use this to verify your work — run the tests, "
            "import the module, execute the script.\n\n"
            f"Allowed executables: {', '.join(sorted(ALLOWED_EXECUTABLES))}.\n\n"
            "There is no shell: pipes, redirects, globs and chaining with && are not "
            "available. Run one program at a time. The command runs with no network "
            "access, so package installs will fail. There is a wall-clock timeout; "
            "long-running servers will be killed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command to run, e.g. 'python -m pytest -q'.",
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Working directory relative to the workspace root. Defaults to '.'."
                    ),
                },
            },
            "required": ["command"],
        },
        handler=run_command,
    )
)
