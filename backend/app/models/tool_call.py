"""ToolCall — one tool invocation, logged before execution.

The row is written *before* the tool runs (spec section 3: "every command is
logged to the DB before execution") and updated with the result afterwards.
A row with a NULL result and NULL error is a call that never returned — a
crash or a hard kill — and that is deliberately visible in the trace.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.timestamps import utcnow


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # The tool_use block id from the API, so a call can be tied to its result.
    tool_use_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    task: Mapped[Task] = relationship(back_populates="tool_calls")  # noqa: F821

    def __repr__(self) -> str:
        return f"<ToolCall {self.id} {self.tool_name} err={bool(self.error)}>"
