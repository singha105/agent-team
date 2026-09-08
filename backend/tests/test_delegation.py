"""Delegation context: hop budget, cycle detection and the shared deadline."""

from __future__ import annotations

import time

import pytest

from app.agents.delegation import (
    DelegationCycle,
    DelegationTimeout,
    HopLimitExceeded,
    new_root_context,
)


def root(max_hops: int = 5, deadline: float = 60.0):
    return new_root_context(
        root_task_id=1, max_hops=max_hops, deadline_seconds=deadline, agent_key="backend"
    )


def test_a_root_context_starts_with_itself_in_the_chain() -> None:
    ctx = root()
    assert ctx.chain == ("backend",)
    assert ctx.hops == 0
    assert ctx.depth == 1


def test_the_hop_budget_is_shared_by_the_whole_tree() -> None:
    """A per-run counter would let every new child reset the budget, which
    makes the limit meaningless."""
    ctx = root()
    child = ctx.descend("database")
    grandchild = child.descend("devops")

    assert ctx.hops == child.hops == grandchild.hops == 2

    grandchild.descend("frontend")
    assert ctx.hops == 3, "a descendant's hop must draw down the root's budget"


def test_the_clock_is_shared_by_the_whole_tree() -> None:
    """Per-run timeouts multiply: ten nested runs at 60s each is ten minutes."""
    ctx = root(deadline=60.0)
    time.sleep(0.05)
    child = ctx.descend("database")
    assert child.counters.started_monotonic == ctx.counters.started_monotonic
    assert child.elapsed >= 0.05


def test_hop_limit_is_enforced() -> None:
    ctx = root(max_hops=2)
    ctx.authorise("database")
    a = ctx.descend("database")
    a.authorise("devops")
    b = a.descend("devops")

    with pytest.raises(HopLimitExceeded) as exc:
        b.authorise("frontend")
    assert exc.value.limit == 2
    assert "finish with what you have" in str(exc.value)


def test_direct_cycle_is_refused() -> None:
    """A -> B -> A. B cannot ask back, because A is blocked waiting on B."""
    ctx = root()
    child = ctx.descend("database")

    with pytest.raises(DelegationCycle) as exc:
        child.authorise("backend")
    assert "backend → database → backend" in str(exc.value)


def test_indirect_cycle_is_refused() -> None:
    """A -> B -> C -> A is the same deadlock, one hop further out."""
    ctx = root()
    grandchild = ctx.descend("database").descend("devops")

    with pytest.raises(DelegationCycle):
        grandchild.authorise("backend")


def test_ping_pong_is_caught_before_it_can_repeat() -> None:
    """The A <-> B loop the spec asks about is refused on the first return
    hop, so it never gets to repeat at all."""
    ctx = root(max_hops=100)
    b = ctx.descend("database")
    with pytest.raises(DelegationCycle):
        b.authorise("backend")
    assert ctx.hops == 1, "the loop was stopped after a single hop"


def test_a_sibling_may_be_asked_twice_across_different_branches() -> None:
    """Cycle detection must not block legitimate fan-out — asking the same
    teammate from two separate branches is fine, since neither is waiting on
    the other."""
    ctx = root(max_hops=10)
    first = ctx.descend("database")
    assert first.chain == ("backend", "database")

    # Back at the root, asking the same agent again is a new branch, not a cycle.
    ctx.authorise("database")
    second = ctx.descend("database")
    assert second.chain == ("backend", "database")
    assert ctx.hops == 2


def test_expired_deadline_refuses_further_delegation() -> None:
    ctx = root(deadline=0.0)
    with pytest.raises(DelegationTimeout):
        ctx.authorise("database")


def test_deadline_is_checked_before_the_hop_limit() -> None:
    """An expired tree should say it ran out of time, not that it ran out of
    hops — the two have different fixes."""
    ctx = root(max_hops=0, deadline=0.0)
    with pytest.raises(DelegationTimeout):
        ctx.authorise("database")


def test_remaining_seconds_decreases() -> None:
    ctx = root(deadline=10.0)
    first = ctx.remaining_seconds
    time.sleep(0.05)
    assert ctx.remaining_seconds < first
    assert not ctx.expired
