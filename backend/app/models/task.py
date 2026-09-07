"""Task — a unit of work assigned to one agent.

`parent_task_id` is populated from Phase 3 onward, when an agent delegates a
sub-task to another agent. In Phase 1 it is always NULL.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import HUMAN, TaskStatus


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=TaskStatus.QUEUED, index=True, nullable=False
    )
    # 'human' or an agent key — who created this task.
    created_by: Mapped[str] = mapped_column(String(64), default=HUMAN, nullable=False)
    parent_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # Populated when status becomes budget_exceeded or failed.
    halt_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The manager's most recent rejection feedback. Kept after a re-queue so the
    # trace shows why the task came back, not just that it did.
    review_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Incremented on every rejection, so attempt 3 is distinguishable from
    # attempt 1 in the trace.
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    assigned_agent: Mapped[Agent] = relationship(  # noqa: F821
        back_populates="tasks", foreign_keys=[assigned_agent_id]
    )
    # Adjacency list: remote_side belongs on the many-to-one side only.
    parent: Mapped[Task | None] = relationship(
        back_populates="subtasks", remote_side=[id], foreign_keys=[parent_task_id]
    )
    subtasks: Mapped[list[Task]] = relationship(
        back_populates="parent", foreign_keys=[parent_task_id]
    )
    messages: Mapped[list[Message]] = relationship(  # noqa: F821
        back_populates="task", cascade="all, delete-orphan", order_by="Message.id"
    )
    tool_calls: Mapped[list[ToolCall]] = relationship(  # noqa: F821
        back_populates="task", cascade="all, delete-orphan", order_by="ToolCall.id"
    )
    usage_records: Mapped[list[Usage]] = relationship(  # noqa: F821
        back_populates="task", cascade="all, delete-orphan", order_by="Usage.id"
    )

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.status} {self.title[:40]!r}>"
