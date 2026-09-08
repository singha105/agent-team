"""Cost and usage attribution across a delegation tree.

A task that asks two teammates for help spends money in three places. Reporting
only the root task's own usage understates what the work cost by however much
the delegation did — which, since children can delegate further, can be most of
it.

The tree is walked with a recursive CTE rather than in Python: the depth is not
known in advance, and a loop issuing one query per level turns a five-deep
delegation into five round trips.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Task, Usage


@dataclass(frozen=True)
class AgentSpend:
    agent_key: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    estimated_cost_usd: float
    task_count: int

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


@dataclass(frozen=True)
class TreeUsage:
    """Everything one root task cost, including every delegated child."""

    root_task_id: int
    task_ids: list[int]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    estimated_cost_usd: float
    by_agent: list[AgentSpend]

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    @property
    def delegated_task_count(self) -> int:
        return max(0, len(self.task_ids) - 1)


def _descendants_cte(root_task_id: int):
    """Recursive CTE yielding the root task id and every descendant."""
    base = select(Task.id.label("id")).where(Task.id == root_task_id).cte("tree", recursive=True)
    return base.union_all(select(Task.id).where(Task.parent_task_id == base.c.id))


async def task_tree_ids(session: AsyncSession, root_task_id: int) -> list[int]:
    """Every task id in the delegation tree, root first."""
    tree = _descendants_cte(root_task_id)
    rows = (await session.execute(select(tree.c.id))).scalars().all()
    # The root leads; the rest are ordered so the trace reads top-down.
    return [root_task_id] + sorted(i for i in rows if i != root_task_id)


async def root_task_id_for(session: AsyncSession, task_id: int) -> int:
    """Walk up parent_task_id to the root of this task's tree."""
    current = task_id
    seen: set[int] = set()
    while True:
        parent = (
            await session.execute(select(Task.parent_task_id).where(Task.id == current))
        ).scalar_one_or_none()
        # `seen` guards against a cycle in the data itself — the delegation
        # limits should prevent one, but a corrupt row must not hang a request.
        if parent is None or parent in seen:
            return current
        seen.add(current)
        current = parent


async def tree_usage(session: AsyncSession, root_task_id: int) -> TreeUsage:
    """Total spend for a root task and every task it delegated."""
    ids = await task_tree_ids(session, root_task_id)

    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(Usage.input_tokens), 0),
                func.coalesce(func.sum(Usage.output_tokens), 0),
                func.coalesce(func.sum(Usage.cache_read_tokens), 0),
                func.coalesce(func.sum(Usage.cache_creation_tokens), 0),
                func.coalesce(func.sum(Usage.estimated_cost_usd), 0.0),
            ).where(Usage.task_id.in_(ids))
        )
    ).one()

    per_agent = (
        await session.execute(
            select(
                Usage.agent_key,
                Usage.model,
                func.coalesce(func.sum(Usage.input_tokens), 0),
                func.coalesce(func.sum(Usage.output_tokens), 0),
                func.coalesce(func.sum(Usage.cache_read_tokens), 0),
                func.coalesce(func.sum(Usage.cache_creation_tokens), 0),
                func.coalesce(func.sum(Usage.estimated_cost_usd), 0.0),
                func.count(func.distinct(Usage.task_id)),
            )
            .where(Usage.task_id.in_(ids))
            .group_by(Usage.agent_key, Usage.model)
            .order_by(func.sum(Usage.estimated_cost_usd).desc())
        )
    ).all()

    return TreeUsage(
        root_task_id=root_task_id,
        task_ids=ids,
        input_tokens=int(totals[0]),
        output_tokens=int(totals[1]),
        cache_read_tokens=int(totals[2]),
        cache_creation_tokens=int(totals[3]),
        estimated_cost_usd=round(float(totals[4]), 6),
        by_agent=[
            AgentSpend(
                agent_key=row[0],
                model=row[1],
                input_tokens=int(row[2]),
                output_tokens=int(row[3]),
                cache_read_tokens=int(row[4]),
                cache_creation_tokens=int(row[5]),
                estimated_cost_usd=round(float(row[6]), 6),
                task_count=int(row[7]),
            )
            for row in per_agent
        ],
    )


async def delegation_tree(session: AsyncSession, root_task_id: int) -> list[dict]:
    """The tree as a flat list with depth, for rendering the delegation chain."""
    ids = await task_tree_ids(session, root_task_id)
    rows = (
        await session.execute(
            select(
                Task.id,
                Task.parent_task_id,
                Task.title,
                Task.status,
                Task.created_by,
                Agent.key,
            )
            .join(Agent, Task.assigned_agent_id == Agent.id)
            .where(Task.id.in_(ids))
            .order_by(Task.id)
        )
    ).all()

    by_parent: dict[int | None, list] = {}
    for row in rows:
        by_parent.setdefault(row[1], []).append(row)

    flat: list[dict] = []

    def walk(parent_id: int | None, depth: int) -> None:
        for row in by_parent.get(parent_id, []):
            flat.append(
                {
                    "task_id": row[0],
                    "parent_task_id": row[1],
                    "depth": depth,
                    "title": row[2],
                    "status": row[3],
                    "created_by": row[4],
                    "agent_key": row[5],
                }
            )
            walk(row[0], depth + 1)

    root_row = next((r for r in rows if r[0] == root_task_id), None)
    if root_row is not None:
        flat.append(
            {
                "task_id": root_row[0],
                "parent_task_id": root_row[1],
                "depth": 0,
                "title": root_row[2],
                "status": root_row[3],
                "created_by": root_row[4],
                "agent_key": root_row[5],
            }
        )
        walk(root_task_id, 1)
    return flat


__all__ = [
    "AgentSpend",
    "TreeUsage",
    "delegation_tree",
    "root_task_id_for",
    "task_tree_ids",
    "tree_usage",
]
