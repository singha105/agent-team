"""Message — one entry in a task's conversation trace.

NULL in `from_agent` or `to_agent` means the human manager. Every Messages API
turn produces rows here, including tool_use and tool_result blocks, so the exact
conversation sent to the model can be replayed from the DB.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.timestamps import utcnow

if TYPE_CHECKING:  # pragma: no cover - resolved by SQLAlchemy at runtime
    from app.models.task import Task


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # NULL means the human manager.
    from_agent: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    to_agent: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # Raw Messages API content blocks, stored verbatim.
    content: Mapped[dict | list] = mapped_column(JSON, nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # Loop iteration this message belongs to; lets the trace viewer group turns.
    iteration: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    task: Mapped[Task] = relationship(back_populates="messages")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Message {self.id} task={self.task_id} {self.message_type}>"
