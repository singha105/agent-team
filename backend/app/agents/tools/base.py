"""Tool registry.

A tool is a name, a JSON schema the model sees, and an async callable. The
runtime looks tools up here by name; nothing about any specific agent lives in
this module, and an agent only gets the tools its YAML grants it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

ToolHandler = Callable[..., Awaitable["ToolOutcome"]]


@dataclass
class ToolOutcome:
    """Result of one tool execution.

    `content` is what the model sees in the tool_result block. `payload` is the
    structured form persisted to tool_calls.result — richer than the text, so
    the trace viewer can show exit codes and timings without re-parsing prose.
    """

    content: str
    payload: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    # Collaboration tools need to know who is calling, on which task, under
    # which delegation budget. Filesystem tools deliberately do not: keeping
    # them context-free means they can be tested and reasoned about in
    # isolation, so the dependency is opt-in rather than universal.
    needs_context: bool = False

    def to_api_schema(self) -> dict[str, Any]:
        """The tool definition sent to the Messages API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    if tool.name in _REGISTRY:
        raise ValueError(f"tool {tool.name!r} is already registered")
    _REGISTRY[tool.name] = tool
    return tool


def get_tool(name: str) -> Tool:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"unknown tool {name!r}; registered: {sorted(_REGISTRY)}") from exc


def registered_names() -> list[str]:
    return sorted(_REGISTRY)


def build_toolset(names: list[str]) -> tuple[list[dict[str, Any]], dict[str, Tool]]:
    """Return (API schemas, name -> Tool) for the tools an agent is granted."""
    tools = [get_tool(n) for n in names]
    return [t.to_api_schema() for t in tools], {t.name: t for t in tools}
