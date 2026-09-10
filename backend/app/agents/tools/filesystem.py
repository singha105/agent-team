"""Filesystem tools. Every path goes through sandbox.resolve_in_workspace."""

from __future__ import annotations

from app.agents.current_agent import current_agent
from app.agents.ownership import may_write
from app.agents.project_context import PROJECT_FILE, is_project_file
from app.agents.sandbox import SandboxViolation, resolve_in_workspace, to_workspace_relative
from app.agents.tools.base import Tool, ToolOutcome, register
from app.agents.workspace_locks import get_workspace_locks
from app.core.config import get_settings

MAX_READ_BYTES = 256_000
MAX_LISTING_ENTRIES = 500


async def read_file(path: str, offset: int = 0, limit: int = 0) -> ToolOutcome:
    """Read a file, optionally a window of it.

    A file over the read limit used to be refused outright, which left the agent
    with no way to see any of it — the limit protected the context window by
    making the file unreadable. `offset` and `limit` turn that into a window the
    agent can move, so a 40,000-line log is usable a screen at a time.

    A truncated result always names the exact next call to make. An agent told
    only that output was cut has to guess how to continue, and guessing costs an
    iteration.
    """
    try:
        target = resolve_in_workspace(path)
    except SandboxViolation as exc:
        return ToolOutcome(content=f"Denied: {exc}", payload={"path": path}, is_error=True)

    if offset < 0 or limit < 0:
        return ToolOutcome(
            content="offset and limit must not be negative.",
            payload={"path": path},
            is_error=True,
        )
    if not target.exists():
        return ToolOutcome(content=f"No such file: {path}", payload={"path": path}, is_error=True)
    if target.is_dir():
        return ToolOutcome(
            content=f"{path} is a directory, not a file. Use list_files.",
            payload={"path": path},
            is_error=True,
        )

    size = target.stat().st_size
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutcome(
            content=f"{path} is not UTF-8 text and cannot be read.",
            payload={"path": path},
            is_error=True,
        )
    except OSError as exc:
        return ToolOutcome(content=f"Could not read {path}: {exc}", is_error=True)

    lines = text.splitlines()
    total = len(lines)

    # A plain read returns the file verbatim. Reassembling it from splitlines()
    # drops a trailing newline, and an agent that reads a file, edits it and
    # writes it back would silently strip it every time — which shows up as a
    # spurious diff on every file it touches.
    if offset == 0 and limit == 0 and len(text) <= MAX_READ_BYTES:
        return ToolOutcome(
            content=text,
            payload={
                "path": path,
                "bytes": size,
                "lines": total,
                "offset": 0,
                "returned_lines": total,
                "truncated": False,
            },
        )

    if total and offset >= total:
        return ToolOutcome(
            content=f"{path} has {total} lines; offset {offset} is past the end.",
            payload={"path": path, "lines": total},
            is_error=True,
        )

    end_line = total if limit == 0 else min(total, offset + limit)
    body = "\n".join(lines[offset:end_line])

    clipped_by_bytes = len(body) > MAX_READ_BYTES
    if clipped_by_bytes:
        body = body[:MAX_READ_BYTES]
        end_line = offset + body.count("\n") + 1

    if clipped_by_bytes or end_line < total:
        body += (
            f"\n\n… truncated. Showing lines {offset + 1}-{end_line} of {total}. "
            f"Continue with read_file(path={path!r}, offset={end_line}, "
            f"limit={limit or 400})."
        )

    return ToolOutcome(
        content=body,
        payload={
            "path": path,
            "bytes": size,
            "lines": total,
            "offset": offset,
            "returned_lines": end_line - offset,
            "truncated": clipped_by_bytes or end_line < total,
        },
    )


async def write_file(path: str, content: str) -> ToolOutcome:
    try:
        target = resolve_in_workspace(path)
    except SandboxViolation as exc:
        return ToolOutcome(content=f"Denied: {exc}", payload={"path": path}, is_error=True)

    # Lanes, enforced rather than requested. The prompts already say to stay in
    # your lane; an agent under pressure to finish will write the schema itself
    # anyway. Checked before the sandbox path rules because the refusal is more
    # useful — it names who to ask.
    writer = current_agent()
    allowed, refusal = may_write(writer, path)
    if not allowed:
        return ToolOutcome(
            content=f"Denied: {refusal}",
            payload={"path": path, "out_of_lane": True, "writer": writer},
            is_error=True,
        )

    # The shared context is append-only, and an enforcement a neighbouring tool
    # can bypass is not an enforcement. Checked on the resolved path so
    # './PROJECT.md' and 'a/../PROJECT.md' cannot slip past it.
    if is_project_file(path):
        return ToolOutcome(
            content=(
                f"Denied: {PROJECT_FILE} is append-only shared context and cannot be "
                "overwritten — it holds decisions the rest of the team is building "
                "against. Use append_project_context to add to it."
            ),
            payload={"path": path, "append_only": True},
            is_error=True,
        )

    if target.is_dir():
        return ToolOutcome(
            content=f"{path} is an existing directory.", payload={"path": path}, is_error=True
        )

    # Serialised per path. Agents run concurrently, and two writing the same
    # file is the one way a run destroys work rather than merely failing. The
    # write and the bookkeeping happen under the same lock, so the check for
    # "who wrote this last" cannot interleave with another writer's.
    locks = get_workspace_locks()
    lock = await locks.acquire(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
        conflict = locks.record_write(target, current_agent(), content)
    except OSError as exc:
        return ToolOutcome(content=f"Could not write {path}: {exc}", is_error=True)
    finally:
        lock.release()

    verb = "Overwrote" if existed else "Created"
    written = len(content.encode("utf-8"))
    detail = f"{verb} {path} ({written} bytes)."

    if conflict is not None:
        # The write succeeded but replaced a different agent's content. Saying
        # so is the point: a conflict resolved invisibly is one you discover in
        # code review, long after the run looked successful.
        when = "at the same moment" if conflict.raced else f"{conflict.seconds_since:.0f}s earlier"
        detail += (
            f" Warning: {conflict.replaced_writer} wrote this file {when} and your write "
            f"has replaced their content. If you needed theirs, read the file and merge "
            f"rather than overwriting again."
        )

    return ToolOutcome(
        content=detail,
        payload={
            "path": path,
            "bytes": written,
            "overwrote": existed,
            "conflict": conflict is not None,
            "replaced_writer": conflict.replaced_writer if conflict else None,
        },
    )


async def list_files(path: str = ".") -> ToolOutcome:
    try:
        target = resolve_in_workspace(path)
    except SandboxViolation as exc:
        return ToolOutcome(content=f"Denied: {exc}", payload={"path": path}, is_error=True)

    if not target.exists():
        return ToolOutcome(
            content=f"No such directory: {path}", payload={"path": path}, is_error=True
        )
    if not target.is_dir():
        return ToolOutcome(
            content=f"{path} is a file, not a directory. Use read_file.",
            payload={"path": path},
            is_error=True,
        )

    settings = get_settings()
    entries: list[str] = []
    truncated = False
    for item in sorted(target.rglob("*")):
        if len(entries) >= MAX_LISTING_ENTRIES:
            truncated = True
            break
        # The whole per-entry block is guarded, not just resolve(): a broken
        # symlink or a file deleted between rglob() and stat() raises OSError
        # here, and one bad entry must not take down the whole listing.
        try:
            rel = to_workspace_relative(item.resolve(), settings)
            entries.append(f"{rel}/" if item.is_dir() else f"{rel}  ({item.stat().st_size}b)")
        except (ValueError, OSError):
            # A symlink pointing outside the workspace, or a dangling one.
            label = (
                "broken symlink"
                if item.is_symlink() and not item.exists()
                else "not accessible from the workspace"
            )
            entries.append(f"{item.name}  [{label}]")

    if not entries:
        return ToolOutcome(content=f"{path} is empty.", payload={"path": path, "count": 0})

    listing = "\n".join(entries)
    if truncated:
        listing += f"\n... listing truncated at {MAX_LISTING_ENTRIES} entries"
    return ToolOutcome(content=listing, payload={"path": path, "count": len(entries)})


register(
    Tool(
        name="read_file",
        description=(
            "Read a UTF-8 text file from the workspace. The path is relative to the "
            "workspace root. Read a file before you modify it.\n\n"
            "If the result is truncated it tells you the exact next call to make: page "
            "through a large file with offset and limit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace root, e.g. 'api/main.py'.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "First line to return, zero-based. Use with limit to page through "
                        "a file too large to read in one call."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Lines to return. 0 reads to the end of the file.",
                },
            },
            "required": ["path"],
        },
        handler=read_file,
    )
)

register(
    Tool(
        name="write_file",
        description=(
            "Write a UTF-8 text file in the workspace, creating parent directories as "
            "needed. Overwrites the file if it exists, so read it first if you are "
            "modifying rather than creating. Write the complete file contents."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace root.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file contents. Not a diff or a fragment.",
                },
            },
            "required": ["path", "content"],
        },
        handler=write_file,
    )
)

register(
    Tool(
        name="list_files",
        description=(
            "Recursively list files and directories in the workspace. Call this first "
            "to see what already exists."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory relative to the workspace root. Defaults to '.'.",
                }
            },
            "required": [],
        },
        handler=list_files,
    )
)
