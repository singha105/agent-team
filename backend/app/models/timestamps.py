"""Timestamp helper.

SQLite's CURRENT_TIMESTAMP has whole-second resolution, so every row written
during one agent iteration shares an identical timestamp. The trace endpoint
then cannot order messages, tool calls and usage rows against each other —
their ids are independent per-table sequences, so a tool call appears before
the message that caused it.

Generating the timestamp in Python gives microsecond precision and makes the
interleaved ordering correct, which is the whole point of the trace viewer.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
