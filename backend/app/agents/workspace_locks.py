"""Serialising workspace writes, and noticing when one clobbers another.

Agents run concurrently, and two of them writing the same file is the one way a
run destroys work rather than merely failing. A lock per resolved path makes
each write atomic, so the loser overwrites cleanly instead of interleaving into
a corrupt file.

Locking alone is not enough, though, and it is worth being precise about why.
Waiting on a lock only detects writes that *overlap in time*, and file writes
are fast — the realistic case is one agent writing a file and another replacing
it thirty seconds later, with no contention at all and the first agent's work
silently gone. So this also remembers who last wrote each path and with what, and
reports a replacement whether or not the two writes raced.

A conflict resolved invisibly is one you discover in code review.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Conflict:
    """One write that replaced another agent's content."""

    path: str
    replaced_writer: str
    seconds_since: float
    raced: bool
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class _Write:
    writer: str
    digest: str
    at: float


class WorkspaceLocks:
    """Per-path locks plus a record of who wrote what."""

    def __init__(self) -> None:
        self._locks: dict[Path, asyncio.Lock] = {}
        self._last: dict[Path, _Write] = {}
        self._conflicts: list[Conflict] = []
        # Guards the dict itself: two coroutines reaching an unlocked path at
        # once would otherwise each create a lock and neither would exclude the
        # other, which is the bug this class exists to prevent.
        self._registry_lock = asyncio.Lock()

    async def _lock_for(self, path: Path) -> asyncio.Lock:
        async with self._registry_lock:
            lock = self._locks.get(path)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[path] = lock
            return lock

    @staticmethod
    def _digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def acquire(self, path: Path) -> asyncio.Lock:
        """Take the lock for `path`. Release it yourself."""
        lock = await self._lock_for(path)
        await lock.acquire()
        return lock

    def record_write(self, path: Path, writer: str, content: str) -> Conflict | None:
        """Note a completed write, returning a Conflict if it replaced someone.

        Called while the path lock is held, so the read-then-write of `_last`
        cannot interleave with another writer's.
        """
        now = asyncio.get_event_loop().time()
        digest = self._digest(content)
        previous = self._last.get(path)
        self._last[path] = _Write(writer=writer, digest=digest, at=now)

        if previous is None:
            return None
        if previous.writer == writer:
            # An agent revising its own file is ordinary work, not a conflict.
            return None
        if previous.digest == digest:
            # Same bytes: nothing was lost.
            return None

        elapsed = max(0.0, now - previous.at)
        conflict = Conflict(
            path=str(path),
            replaced_writer=previous.writer,
            seconds_since=elapsed,
            raced=elapsed < 0.05,
        )
        self._conflicts.append(conflict)
        return conflict

    @property
    def conflicts(self) -> list[Conflict]:
        return list(self._conflicts)

    def reset(self) -> None:
        """Test hook."""
        self._locks.clear()
        self._last.clear()
        self._conflicts.clear()


_locks: WorkspaceLocks | None = None


def get_workspace_locks() -> WorkspaceLocks:
    global _locks
    if _locks is None:
        _locks = WorkspaceLocks()
    return _locks
