"""Filesystem tools. Every path goes through sandbox.resolve_in_workspace."""

from __future__ import annotations

from app.agents.sandbox import SandboxViolation, resolve_in_workspace, to_workspace_relative
from app.agents.tools.base import Tool, ToolOutcome, register
from app.core.config import get_settings

MAX_READ_BYTES = 256_000
MAX_LISTING_ENTRIES = 500


async def read_file(path: str) -> ToolOutcome:
    try:
        target = resolve_in_workspace(path)
    except SandboxViolation as exc:
        return ToolOutcome(content=f"Denied: {exc}", payload={"path": path}, is_error=True)

    if not target.exists():
        return ToolOutcome(content=f"No such file: {path}", payload={"path": path}, is_error=True)
    if target.is_dir():
        return ToolOutcome(
            content=f"{path} is a directory, not a file. Use list_files.",
            payload={"path": path},
            is_error=True,
        )

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        return ToolOutcome(
            content=f"{path} is {size} bytes, over the {MAX_READ_BYTES} byte read limit.",
            payload={"path": path, "size": size},
            is_error=True,
        )

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

    return ToolOutcome(content=text, payload={"path": path, "bytes": size})


async def write_file(path: str, content: str) -> ToolOutcome:
    try:
        target = resolve_in_workspace(path)
    except SandboxViolation as exc:
        return ToolOutcome(content=f"Denied: {exc}", payload={"path": path}, is_error=True)

    if target.is_dir():
        return ToolOutcome(
            content=f"{path} is an existing directory.", payload={"path": path}, is_error=True
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolOutcome(content=f"Could not write {path}: {exc}", is_error=True)

    verb = "Overwrote" if existed else "Created"
    written = len(content.encode("utf-8"))
    return ToolOutcome(
        content=f"{verb} {path} ({written} bytes).",
        payload={"path": path, "bytes": written, "overwrote": existed},
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
        # A symlink pointing outside is listed as such but never followed.
        try:
            rel = to_workspace_relative(item.resolve(), settings)
        except (ValueError, OSError):
            entries.append(f"{item.name}  [symlink outside workspace — not accessible]")
            continue
        entries.append(f"{rel}/" if item.is_dir() else f"{rel}  ({item.stat().st_size}b)")

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
            "workspace root. Read a file before you modify it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace root, e.g. 'api/main.py'.",
                }
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
