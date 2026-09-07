"""String enums shared by the ORM models.

Stored as plain strings in SQLite so the DB stays readable with a bare
`sqlite3` shell — the trace is meant to be inspectable without this codebase.
"""

from __future__ import annotations

from enum import StrEnum

HUMAN = "human"


class TaskStatus(StrEnum):
    """Task lifecycle states.

    Phase 2 renamed the Phase 1 vocabulary (pending/running/completed) to
    match the spec's queued/in_progress/done and added needs_review. Existing
    rows are migrated; see the alembic revision for the mapping.
    """

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    NEEDS_REVIEW = "needs_review"
    DONE = "done"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"


class AgentStatus(StrEnum):
    """What an agent is doing right now. Drives the UI in later phases."""

    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"
    WAITING_ON_HUMAN = "waiting_on_human"
    BLOCKED = "blocked"
    ERROR = "error"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class MessageType(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    AGENT_TO_AGENT = "agent_to_agent"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
