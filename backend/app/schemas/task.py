"""Request and response models for the task API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_key: str = Field(min_length=1, description="Which agent to assign this to.")
    description: str = Field(min_length=1, description="What the agent should do.")
    title: str | None = Field(
        default=None,
        max_length=512,
        description="Optional; derived from the description if absent.",
    )

    @field_validator("description", "agent_key")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    feedback: str | None = Field(
        default=None,
        description="Required when rejecting: it is appended to the conversation as your next "
        "message, so it has to say what to change.",
    )

    @field_validator("feedback")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if v else None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    from_agent: str | None
    to_agent: str | None
    role: str
    message_type: str
    content: Any
    iteration: int
    created_at: datetime


class ToolCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    agent_key: str
    tool_name: str
    tool_use_id: str | None
    arguments: dict
    result: dict | None
    duration_ms: int | None
    error: str | None
    created_at: datetime


class UsageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    iteration: int
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    estimated_cost_usd: float
    created_at: datetime


class TaskSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str
    status: str
    agent_key: str
    created_by: str
    parent_task_id: int | None
    halt_reason: str | None
    review_feedback: str | None
    attempt: int
    created_at: datetime
    updated_at: datetime


class AgentSpendOut(BaseModel):
    agent_key: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    task_count: int


class TreeUsageOut(BaseModel):
    """Spend for a task and everything it delegated.

    Reporting only a task's own usage understates the work by however much the
    delegation cost, which — since children delegate further — can be most of it.
    """

    root_task_id: int
    task_ids: list[int]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    delegated_task_count: int
    by_agent: list[AgentSpendOut] = Field(default_factory=list)


class DelegationNode(BaseModel):
    task_id: int
    parent_task_id: int | None
    depth: int
    title: str
    status: str
    created_by: str
    agent_key: str


class TaskDetail(TaskSummary):
    messages: list[MessageOut] = Field(default_factory=list)
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    usage: list[UsageOut] = Field(default_factory=list)
    # This task alone.
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    # This task plus everything it delegated.
    tree: TreeUsageOut | None = None
    delegation: list[DelegationNode] = Field(default_factory=list)


class TraceEntry(BaseModel):
    """One ordered step in a run. Kinds interleave in real time order."""

    kind: Literal["message", "tool_call", "usage"]
    at: datetime
    iteration: int
    ref_id: int
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)


class TaskTrace(BaseModel):
    task_id: int
    status: str
    agent_key: str
    attempt: int
    entries: list[TraceEntry]
    total_cost_usd: float
    total_tokens: int
    tree: TreeUsageOut | None = None
    delegation: list[DelegationNode] = Field(default_factory=list)
