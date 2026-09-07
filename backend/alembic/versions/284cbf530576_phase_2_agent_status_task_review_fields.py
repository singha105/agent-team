"""phase 2: agent status, task review fields, lifecycle status rename

Revision ID: 284cbf530576
Revises: e6b85cbd5658
Create Date: 2026-09-07

Hand-edited after autogenerate, for two reasons the generator cannot know about:

  1. The new NOT NULL columns need a server_default, or the migration fails on
     any table that already has rows.
  2. Phase 2 renamed the task status vocabulary. Autogenerate sees no schema
     change for that — status is a plain string column — so the existing row
     VALUES have to be migrated explicitly or every historical task keeps a
     status the state machine no longer recognises.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "284cbf530576"
down_revision: str | None = "e6b85cbd5658"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Phase 1 vocabulary -> Phase 2 vocabulary.
#
# 'completed' becomes 'done' rather than 'needs_review': those runs finished
# under Phase 1 rules, where there was no review step to perform. Retroactively
# marking them as awaiting review would invent work that was never pending.
STATUS_RENAMES = [
    ("pending", "queued"),
    ("running", "in_progress"),
    ("completed", "done"),
]


def upgrade() -> None:
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(length=32), nullable=False, server_default="idle")
        )
        batch_op.add_column(
            sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(batch_op.f("ix_agents_status"), ["status"], unique=False)

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("review_feedback", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1")
        )

    for old, new in STATUS_RENAMES:
        op.execute(
            sa.text("UPDATE tasks SET status = :new WHERE status = :old").bindparams(
                new=new, old=old
            )
        )


def downgrade() -> None:
    for old, new in STATUS_RENAMES:
        op.execute(
            sa.text("UPDATE tasks SET status = :old WHERE status = :new").bindparams(
                new=new, old=old
            )
        )
    # needs_review has no Phase 1 equivalent; the closest honest mapping is the
    # state those runs would have held before the review step existed.
    op.execute(sa.text("UPDATE tasks SET status = 'completed' WHERE status = 'needs_review'"))

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("attempt")
        batch_op.drop_column("reviewed_at")
        batch_op.drop_column("review_feedback")

    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_agents_status"))
        batch_op.drop_column("status_changed_at")
        batch_op.drop_column("status")
