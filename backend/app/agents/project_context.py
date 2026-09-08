"""Shared project context — workspace/PROJECT.md.

The one document every agent reads before starting and may add to. It is where
the schema, the API contract and the conventions live, so the Frontend agent
builds against what the Backend agent actually published rather than something
it inferred.

Append-only, and enforced here rather than requested in a prompt. Two agents
working in the same tree would otherwise overwrite each other's decisions, and
the record of who decided what — the reason this file exists — would be exactly
what got lost. `write_file` refuses this path for the same reason; an
enforcement a neighbouring tool can bypass is not an enforcement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.agents.sandbox import resolve_in_workspace, workspace_root
from app.core.config import Settings, get_settings

PROJECT_FILE = "PROJECT.md"
MAX_SECTION_CHARS = 20_000
MAX_FILE_CHARS = 400_000

HEADER = """# PROJECT.md — shared team context

Append-only. Every agent reads this before starting and adds what the rest of
the team needs: the schema, the API contract, conventions and decisions.

Do not restate what is already here. Add what is missing, and say who decided it.
"""


def project_file_path(settings: Settings | None = None) -> Path:
    return workspace_root(settings or get_settings()) / PROJECT_FILE


def is_project_file(path: str, settings: Settings | None = None) -> bool:
    """Whether an agent-supplied path points at PROJECT.md.

    Resolved rather than string-compared, so `./PROJECT.md`, `a/../PROJECT.md`
    and the bare name all match and none of them slip past the guard.
    """
    try:
        return resolve_in_workspace(path, settings) == project_file_path(settings)
    except Exception:  # noqa: BLE001 - any resolution failure means 'not this file'
        return False


def ensure_exists(settings: Settings | None = None) -> Path:
    path = project_file_path(settings)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER, encoding="utf-8")
    return path


def read_context(settings: Settings | None = None) -> str:
    path = project_file_path(settings)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def append_section(
    agent_key: str, heading: str, body: str, settings: Settings | None = None
) -> tuple[bool, str]:
    """Append one section. Returns (ok, detail).

    Never truncates, never rewrites, and never reorders. The only supported
    change to this file is more of it.
    """
    heading = (heading or "").strip()
    body = (body or "").strip()

    if not heading:
        return False, "a heading is required so the team can find this later"
    if not body:
        return False, "an empty note helps nobody; write what you decided"
    if len(body) > MAX_SECTION_CHARS:
        return (
            False,
            f"section is {len(body)} characters, over the {MAX_SECTION_CHARS} limit. "
            "Summarise the decision rather than pasting the whole artefact.",
        )

    path = ensure_exists(settings)
    existing = path.read_text(encoding="utf-8")

    if len(existing) + len(body) > MAX_FILE_CHARS:
        return (
            False,
            f"{PROJECT_FILE} is at its {MAX_FILE_CHARS} character limit. "
            "The shared context is full; summarise instead of appending.",
        )

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    section = f"\n\n---\n\n## {heading}\n\n_added by **{agent_key}** at {stamp}_\n\n{body}\n"

    # Append mode, not read-modify-write: two agents appending at once cannot
    # clobber each other, and a crash mid-write cannot lose what was there.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(section)

    return True, f"Appended '{heading}' to {PROJECT_FILE} ({len(body)} characters)."


def context_prelude(settings: Settings | None = None) -> str:
    """The block injected into an agent's first message.

    Injected rather than left for the agent to fetch: 'read PROJECT.md first' is
    an instruction agents skip under pressure, and the whole point is that they
    build against published decisions instead of invented ones.
    """
    content = read_context(settings).strip()
    if not content or content.strip() == HEADER.strip():
        return (
            f"## Shared context ({PROJECT_FILE})\n\n"
            "The shared context is empty — you are first. If you decide anything the "
            "rest of the team must build against (a schema, an API contract, a "
            "convention), record it with `append_project_context`."
        )
    return (
        f"## Shared context ({PROJECT_FILE})\n\n"
        "This is what your teammates have already published. Build against it "
        "rather than inventing your own version, and if something you need is "
        "missing, ask the agent who owns it.\n\n"
        f"{content}"
    )
