"""Agent — the DB projection of a config/teams/<team>/*.yaml file.

The YAML is the source of truth. Rows here exist so that tasks, messages and
usage can foreign-key to a stable id, and so a run can be reconstructed even
after the config changes. Rows are synced from YAML, never edited by hand.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import AgentStatus
from app.models.timestamps import utcnow


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(512), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    avatar_id: Mapped[str] = mapped_column(String(64), nullable=False)
    system_prompt_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # What the agent is doing right now. Distinct from task status: an agent is
    # idle between tasks, and a task can be needs_review while its agent has
    # already moved on.
    status: Mapped[str] = mapped_column(
        String(32), default=AgentStatus.IDLE, index=True, nullable=False
    )
    status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    tasks: Mapped[list[Task]] = relationship(  # noqa: F821
        back_populates="assigned_agent", foreign_keys="Task.assigned_agent_id"
    )

    def __repr__(self) -> str:
        return f"<Agent {self.key} ({self.model})>"
