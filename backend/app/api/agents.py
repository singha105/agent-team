"""Agent roster API.

Identity comes from YAML; the DB row supplies live status. Both are merged
here so a client gets one coherent view without knowing that split exists.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.config_loader import AgentConfigError, load_all_agent_configs
from app.core.db import get_db
from app.models import Agent, AgentStatus, Task, TaskStatus
from app.schemas.agent import AgentOut

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("", response_model=list[AgentOut])
async def list_agents(session: AsyncSession = Depends(get_db)) -> list[AgentOut]:
    """The roster with each agent's current status and workload."""
    try:
        configs = load_all_agent_configs()
    except AgentConfigError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    rows = {row.key: row for row in (await session.execute(select(Agent))).scalars().all()}

    queued_counts = dict(
        (
            await session.execute(
                select(Agent.key, func.count(Task.id))
                .join(Task, Task.assigned_agent_id == Agent.id)
                .where(Task.status.in_([TaskStatus.QUEUED, TaskStatus.IN_PROGRESS]))
                .group_by(Agent.key)
            )
        ).all()
    )

    active = dict(
        (
            await session.execute(
                select(Agent.key, func.max(Task.id))
                .join(Task, Task.assigned_agent_id == Agent.id)
                .where(Task.status == TaskStatus.IN_PROGRESS)
                .group_by(Agent.key)
            )
        ).all()
    )

    roster: list[AgentOut] = []
    for key, config in sorted(configs.items()):
        row = rows.get(key)
        roster.append(
            AgentOut(
                key=config.key,
                display_name=config.display_name,
                role=config.role,
                model=config.model,
                avatar_id=config.avatar_id,
                # An agent with no row has never been dispatched to, which is
                # idle in every sense that matters to a client.
                status=row.status if row else AgentStatus.IDLE,
                status_changed_at=row.status_changed_at if row else None,
                bio=config.bio,
                personality=config.personality,
                owns=list(config.owns),
                tools=list(config.tools),
                active_task_id=active.get(key),
                queued_tasks=queued_counts.get(key, 0),
            )
        )
    return roster
