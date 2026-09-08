"""Shared state for one delegation tree.

When the Backend agent asks the Database agent for a schema, and that agent asks
someone else, all of those runs belong to a single unit of work. The limits that
matter are properties of the whole tree, not of any one run:

  hops     — how many times control has crossed from one agent to another,
             counted against the root task, not per-runtime. Otherwise every
             new child resets the budget and the limit means nothing.
  chain    — the path of agents from the root to the current run, used to catch
             A → B → A before it becomes A → B → A → B forever.
  deadline — one wall clock for the entire tree. Per-run timeouts multiply: ten
             nested runs at 60s each is a ten-minute stall, not a one-minute one.

One of these is created for a root task and passed down unchanged, so a child
cannot widen a limit its parent was already bound by.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class DelegationError(RuntimeError):
    """Base for delegation refusals. These are reported to the calling agent as
    tool errors, not raised through the loop — the agent has to be able to see
    the refusal and carry on without the answer."""


class HopLimitExceeded(DelegationError):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(
            f"inter-agent hop limit of {limit} reached for this task. "
            "No further delegation is possible; finish with what you have, or "
            "explain what you still need and stop."
        )


class DelegationCycle(DelegationError):
    def __init__(self, chain: tuple[str, ...], target: str) -> None:
        self.chain = chain
        self.target = target
        path = " → ".join([*chain, target])
        super().__init__(
            f"delegation cycle refused: {path}. {target!r} is already waiting "
            "earlier in this chain, so asking it again would deadlock. Answer "
            "from what you already know, or say what is missing."
        )


class DelegationTimeout(DelegationError):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(
            f"the {seconds:.0f}s wall-clock budget for this task tree is spent. "
            "Stop delegating and finish with what you have."
        )


@dataclass
class _TreeCounters:
    """State shared by every context in one tree, by reference."""

    hops: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)


@dataclass
class DelegationContext:
    """One agent's view of the delegation tree it belongs to.

    Per-context: which agents are waiting above this run. Shared by reference
    through `counters`: the hop budget and the clock, so a child cannot reset a
    limit its parent was already bound by.
    """

    root_task_id: int
    max_hops: int
    deadline_seconds: float
    # Agents currently waiting, root first. The last entry is the running agent.
    chain: tuple[str, ...] = ()
    counters: _TreeCounters = field(default_factory=_TreeCounters)

    @property
    def hops(self) -> int:
        return self.counters.hops

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.counters.started_monotonic

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_seconds - self.elapsed)

    @property
    def expired(self) -> bool:
        return self.remaining_seconds <= 0

    @property
    def depth(self) -> int:
        return len(self.chain)

    @property
    def current_agent(self) -> str | None:
        return self.chain[-1] if self.chain else None

    def check_deadline(self) -> None:
        if self.expired:
            raise DelegationTimeout(self.deadline_seconds)

    def authorise(self, target: str) -> None:
        """Decide whether the current agent may delegate to `target`.

        Raises rather than returning a flag: every refusal carries a reason the
        agent is shown verbatim, and a bare False would lose it.
        """
        self.check_deadline()

        if self.counters.hops >= self.max_hops:
            raise HopLimitExceeded(self.max_hops)

        # A target already in the chain is waiting on this very call, so asking
        # it would deadlock — this is the A → B → A case, caught one hop before
        # it becomes an A ↔ B ping-pong.
        if target in self.chain:
            raise DelegationCycle(self.chain, target)

    def descend(self, target: str) -> DelegationContext:
        """Record a hop and return the context the child runs under."""
        self.counters.hops += 1
        return DelegationContext(
            root_task_id=self.root_task_id,
            max_hops=self.max_hops,
            deadline_seconds=self.deadline_seconds,
            chain=(*self.chain, target),
            counters=self.counters,  # shared by reference: one budget per tree
        )


def new_root_context(
    root_task_id: int, max_hops: int, deadline_seconds: float, agent_key: str
) -> DelegationContext:
    return DelegationContext(
        root_task_id=root_task_id,
        max_hops=max_hops,
        deadline_seconds=deadline_seconds,
        chain=(agent_key,),
    )


async def restore_context(
    session, task, max_hops: int, deadline_seconds: float, agent_key: str
) -> DelegationContext:
    """Rebuild a tree's delegation state from the database.

    An in-memory context is passed down through nested `ask_agent` calls, but a
    task the worker picks up later — a notification that woke an idle agent, or
    a rejected task re-queued for another attempt — has no such parent to
    inherit from. Reconstructing from persisted state keeps one budget across
    the whole tree even when it spans process boundaries.

    Three things are derived rather than stored, so there is no second copy to
    fall out of step with the trace:

      hops     — one per agent-to-agent message already recorded in the tree
      chain    — the agent keys from the root down to this task
      deadline — measured from the root task's creation, not from now, so a
                 woken task cannot restart the clock
    """
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    from app.agents.rollup import root_task_id_for, task_tree_ids
    from app.models import Agent, Message, MessageType, Task

    root_id = await root_task_id_for(session, task.id)
    tree_ids = await task_tree_ids(session, root_id)

    hops = (
        await session.execute(
            select(func.count())
            .select_from(Message)
            .where(
                Message.task_id.in_(tree_ids),
                Message.message_type == MessageType.AGENT_TO_AGENT,
            )
        )
    ).scalar_one() or 0

    # The chain is the ancestry: root agent first, this one last.
    chain: list[str] = []
    current: int | None = task.id
    seen: set[int] = set()
    while current is not None and current not in seen:
        seen.add(current)
        row = (
            await session.execute(
                select(Agent.key, Task.parent_task_id)
                .join(Task, Task.assigned_agent_id == Agent.id)
                .where(Task.id == current)
            )
        ).one_or_none()
        if row is None:
            break
        chain.append(row[0])
        current = row[1]
    chain.reverse()
    if not chain:
        chain = [agent_key]

    root_created = (
        await session.execute(select(Task.created_at).where(Task.id == root_id))
    ).scalar_one_or_none()

    counters = _TreeCounters(hops=int(hops))
    if root_created is not None:
        if root_created.tzinfo is None:
            root_created = root_created.replace(tzinfo=UTC)
        already_spent = (datetime.now(UTC) - root_created).total_seconds()
        # Shift the monotonic origin back so `elapsed` reflects the age of the
        # tree rather than the age of this process's context object.
        counters.started_monotonic = time.monotonic() - max(0.0, already_spent)

    return DelegationContext(
        root_task_id=root_id,
        max_hops=max_hops,
        deadline_seconds=deadline_seconds,
        chain=tuple(chain),
        counters=counters,
    )
