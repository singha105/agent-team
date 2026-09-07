"""String enums shared by the ORM models.

Stored as plain strings in SQLite so the DB stays readable with a bare
`sqlite3` shell — the trace is meant to be inspectable without this codebase.
"""

from __future__ import annotations

from enum import StrEnum

HUMAN = "human"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"


class MessageType(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    AGENT_TO_AGENT = "agent_to_agent"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
