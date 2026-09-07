"""Generate TypeScript types for the WebSocket event stream.

Run from the repo root:

    python scripts/export_types.py

Generated from the Pydantic models in app.events.schemas, so the wire format is
defined once. Hand-maintaining a second copy in TypeScript guarantees the two
drift, and the drift shows up as a runtime bug in the browser rather than a
type error anywhere.

CI checks the committed file matches; see `--check`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.events.schemas import EVENT_MODELS  # noqa: E402
from app.models.enums import AgentStatus, TaskStatus  # noqa: E402

OUTPUT = REPO_ROOT / "frontend" / "src" / "lib" / "events.ts"

PRIMITIVES: dict[Any, str] = {
    str: "string",
    int: "number",
    float: "number",
    bool: "boolean",
    datetime: "string",
    type(None): "null",
}


def ts_type(annotation: Any) -> str:
    """Map a Python annotation to a TypeScript type."""
    if annotation in PRIMITIVES:
        return PRIMITIVES[annotation]

    origin = get_origin(annotation)

    if origin is Literal:
        return " | ".join(f'"{a}"' for a in get_args(annotation))

    if origin in (Union, UnionType):
        parts = [ts_type(a) for a in get_args(annotation)]
        # Collapse the common Optional[T] case rather than emitting "T | null | null".
        seen: list[str] = []
        for p in parts:
            if p not in seen:
                seen.append(p)
        return " | ".join(seen)

    if origin in (list, set, tuple):
        args = get_args(annotation)
        return f"{ts_type(args[0])}[]" if args else "unknown[]"

    if origin is dict:
        args = get_args(annotation)
        return (
            f"Record<{ts_type(args[0])}, {ts_type(args[1])}>" if args else "Record<string, unknown>"
        )

    if annotation is Any:
        return "unknown"

    return "unknown"


def render_interface(model: type) -> str:
    name = model.__name__
    lines = [f"export interface {name} {{"]
    for field_name, field in model.model_fields.items():
        rendered = ts_type(field.annotation)
        # Fields with a default are always present on the wire — Pydantic
        # serialises them — so they are not optional in TypeScript.
        doc = field.description
        if doc:
            lines.append(f"  /** {doc} */")
        lines.append(f"  {field_name}: {rendered};")
    lines.append("}")
    return "\n".join(lines)


def render_enum(name: str, enum_cls: type) -> str:
    values = " | ".join(f'"{m.value}"' for m in enum_cls)
    return f"export type {name} = {values};"


def generate() -> str:
    parts = [
        "// AUTO-GENERATED — do not edit by hand.",
        "// Source: backend/app/events/schemas.py",
        "// Regenerate: python scripts/export_types.py",
        "",
        render_enum("TaskStatus", TaskStatus),
        render_enum("AgentStatus", AgentStatus),
        "",
    ]
    parts.extend(render_interface(m) + "\n" for m in EVENT_MODELS)

    # Built without a backslash inside an f-string expression: that is 3.12+
    # syntax and this project supports 3.11.
    members = "\n  | ".join(m.__name__ for m in EVENT_MODELS)
    parts.append("export type AgentTeamEvent =\n  | " + members + ";")
    parts.append("")
    parts.append(
        "export const EVENT_TYPES = [\n"
        + "".join(
            f'  "{m.model_fields["type"].default}",\n' for m in EVENT_MODELS  # type: ignore[index]
        )
        + "] as const;"
    )
    parts.append("")
    parts.append(
        "/** Narrow an event by its discriminant. */\n"
        'export function isEvent<T extends AgentTeamEvent["type"]>(\n'
        "  event: AgentTeamEvent,\n"
        "  type: T,\n"
        "): event is Extract<AgentTeamEvent, { type: T }> {\n"
        "  return event.type === type;\n"
        "}"
    )
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed file is out of date, without writing.",
    )
    args = parser.parse_args()

    generated = generate()

    if args.check:
        if not OUTPUT.exists():
            print(f"{OUTPUT} does not exist; run python scripts/export_types.py", file=sys.stderr)
            return 1
        if OUTPUT.read_text(encoding="utf-8") != generated:
            print(
                f"{OUTPUT} is out of date with backend/app/events/schemas.py.\n"
                "Run: python scripts/export_types.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT.relative_to(REPO_ROOT)} is up to date")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generated, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
