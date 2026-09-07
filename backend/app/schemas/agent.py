"""Response models for the agent roster."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    display_name: str
    role: str
    model: str
    avatar_id: str
    status: str
    status_changed_at: datetime | None = None
    # From YAML rather than the DB row — identity is config, the row is a projection.
    bio: str | None = None
    personality: str | None = None
    owns: list[str] = []
    tools: list[str] = []
    active_task_id: int | None = None
    queued_tasks: int = 0
