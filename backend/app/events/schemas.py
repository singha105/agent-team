"""Typed WebSocket events.

Every event is a Pydantic model in one discriminated union keyed on `type`, so
the frontend can switch exhaustively and the TypeScript types can be generated
from these definitions rather than hand-maintained in two places.

Field names are snake_case to match the REST payloads; the generator carries
them through unchanged so there is one vocabulary across the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(UTC)


class BaseEvent(BaseModel):
    """Common envelope. `seq` is assigned by the bus, not the producer."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(default=0, description="Monotonic per-process sequence number.")
    at: datetime = Field(default_factory=_now)


class AgentStatusChanged(BaseEvent):
    type: Literal["agent.status_changed"] = "agent.status_changed"
    agent_key: str
    status: str
    previous_status: str | None = None
    task_id: int | None = None


class TaskCreated(BaseEvent):
    type: Literal["task.created"] = "task.created"
    task_id: int
    agent_key: str
    title: str
    status: str
    created_by: str


class TaskStatusChanged(BaseEvent):
    type: Literal["task.status_changed"] = "task.status_changed"
    task_id: int
    agent_key: str
    status: str
    previous_status: str
    halt_reason: str | None = None
    attempt: int = 1


class MessageCreated(BaseEvent):
    type: Literal["message.created"] = "message.created"
    task_id: int
    message_id: int
    message_type: str
    role: str
    from_agent: str | None = None
    to_agent: str | None = None
    iteration: int = 0
    # A short excerpt, not the full body: the WebSocket is a notification
    # channel, and clients fetch the full trace over REST.
    preview: str | None = None


class ToolStarted(BaseEvent):
    type: Literal["tool.started"] = "tool.started"
    task_id: int
    agent_key: str
    tool_call_id: int
    tool_name: str
    arguments_preview: str | None = None


class ToolFinished(BaseEvent):
    type: Literal["tool.finished"] = "tool.finished"
    task_id: int
    agent_key: str
    tool_call_id: int
    tool_name: str
    duration_ms: int | None = None
    is_error: bool = False
    error: str | None = None


class UsageUpdated(BaseEvent):
    type: Literal["usage.updated"] = "usage.updated"
    task_id: int
    agent_key: str
    model: str
    iteration: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    task_total_cost_usd: float


AgentTeamEvent = Annotated[
    AgentStatusChanged
    | TaskCreated
    | TaskStatusChanged
    | MessageCreated
    | ToolStarted
    | ToolFinished
    | UsageUpdated,
    Field(discriminator="type"),
]

EVENT_MODELS: tuple[type[BaseEvent], ...] = (
    AgentStatusChanged,
    TaskCreated,
    TaskStatusChanged,
    MessageCreated,
    ToolStarted,
    ToolFinished,
    UsageUpdated,
)

EVENT_TYPES: tuple[str, ...] = tuple(m.model_fields["type"].default for m in EVENT_MODELS)
