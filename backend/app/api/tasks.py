"""Task REST API."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.config_loader import AgentConfigError, get_agent_config
from app.agents.lifecycle import IllegalTransitionError, validate_transition
from app.agents.rollup import delegation_tree, tree_usage
from app.agents.runtime import sync_agent_row
from app.core.db import get_db
from app.events.bus import get_event_bus
from app.events.schemas import MessageCreated, TaskCreated, TaskStatusChanged
from app.models import HUMAN, Agent, Message, MessageType, Task, TaskStatus, ToolCall, Usage
from app.schemas.task import (
    AgentSpendOut,
    DelegationNode,
    MessageOut,
    ReviewRequest,
    TaskCreateRequest,
    TaskDetail,
    TaskSummary,
    TaskTrace,
    ToolCallOut,
    TraceEntry,
    TreeUsageOut,
    UsageOut,
)
from app.workers.queue import get_worker_pool

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

TITLE_MAX = 120


async def _load_task(session: AsyncSession, task_id: int) -> Task:
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no task with id {task_id}")
    return task


async def _agent_key_for(session: AsyncSession, task: Task) -> str:
    agent = (
        await session.execute(select(Agent).where(Agent.id == task.assigned_agent_id))
    ).scalar_one()
    return agent.key


def _summary(task: Task, agent_key: str) -> TaskSummary:
    return TaskSummary(
        id=task.id,
        title=task.title,
        description=task.description,
        status=task.status,
        agent_key=agent_key,
        created_by=task.created_by,
        parent_task_id=task.parent_task_id,
        halt_reason=task.halt_reason,
        review_feedback=task.review_feedback,
        attempt=task.attempt,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def _tree_rollup(session: AsyncSession, task: Task) -> tuple[TreeUsageOut, list]:
    """Usage and shape for this task's delegation tree.

    Rooted at the task itself rather than at the tree's true root: asking about
    a child should report what that child and its own descendants cost, not the
    whole unrelated tree above it.
    """
    usage = await tree_usage(session, task.id)
    nodes = await delegation_tree(session, task.id)
    return (
        TreeUsageOut(
            root_task_id=usage.root_task_id,
            task_ids=usage.task_ids,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_usd=usage.estimated_cost_usd,
            delegated_task_count=usage.delegated_task_count,
            by_agent=[
                AgentSpendOut(
                    agent_key=a.agent_key,
                    model=a.model,
                    input_tokens=a.input_tokens,
                    output_tokens=a.output_tokens,
                    total_tokens=a.total_tokens,
                    estimated_cost_usd=a.estimated_cost_usd,
                    task_count=a.task_count,
                )
                for a in usage.by_agent
            ],
        ),
        [DelegationNode(**n) for n in nodes],
    )


@router.post("", response_model=TaskSummary, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreateRequest, session: AsyncSession = Depends(get_db)
) -> TaskSummary:
    """Create a task and dispatch it.

    Returns as soon as the row exists. Execution happens in the worker pool, so
    a long agent run never holds the HTTP request open.
    """
    try:
        config = get_agent_config(payload.agent_key)
    except AgentConfigError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    agent_row = await sync_agent_row(session, config)
    title = payload.title or (
        payload.description
        if len(payload.description) <= TITLE_MAX
        else payload.description[: TITLE_MAX - 1] + "…"
    )
    task = Task(
        title=title,
        description=payload.description,
        assigned_agent_id=agent_row.id,
        created_by=HUMAN,
        status=TaskStatus.QUEUED,
    )
    session.add(task)
    await session.flush()

    bus = get_event_bus()
    await bus.publish(
        TaskCreated(
            task_id=task.id,
            agent_key=config.key,
            title=task.title,
            status=task.status,
            created_by=task.created_by,
        )
    )
    # Commit before dispatch: the worker opens its own session and would not
    # see an uncommitted row.
    await session.commit()
    await session.refresh(task)
    await get_worker_pool().submit(task.id, config.key)

    return _summary(task, config.key)


@router.get("", response_model=list[TaskSummary])
async def list_tasks(
    session: AsyncSession = Depends(get_db),
    agent_key: str | None = Query(default=None),
    task_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[TaskSummary]:
    stmt = select(Task, Agent.key).join(Agent, Task.assigned_agent_id == Agent.id)
    if agent_key:
        stmt = stmt.where(Agent.key == agent_key)
    if task_status:
        stmt = stmt.where(Task.status == task_status)
    rows = (await session.execute(stmt.order_by(Task.id.desc()).limit(limit))).all()
    return [_summary(task, key) for task, key in rows]


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(task_id: int, session: AsyncSession = Depends(get_db)) -> TaskDetail:
    """One task with its full message and tool-call history."""
    task = await _load_task(session, task_id)
    agent_key = await _agent_key_for(session, task)

    messages = (
        (
            await session.execute(
                select(Message).where(Message.task_id == task_id).order_by(Message.id)
            )
        )
        .scalars()
        .all()
    )
    tool_calls = (
        (
            await session.execute(
                select(ToolCall).where(ToolCall.task_id == task_id).order_by(ToolCall.id)
            )
        )
        .scalars()
        .all()
    )
    usage_rows = (
        (await session.execute(select(Usage).where(Usage.task_id == task_id).order_by(Usage.id)))
        .scalars()
        .all()
    )

    tree, delegation = await _tree_rollup(session, task)

    return TaskDetail(
        **_summary(task, agent_key).model_dump(),
        tree=tree,
        delegation=delegation,
        messages=[MessageOut.model_validate(m) for m in messages],
        tool_calls=[ToolCallOut.model_validate(c) for c in tool_calls],
        usage=[UsageOut.model_validate(u) for u in usage_rows],
        total_cost_usd=round(sum(u.estimated_cost_usd for u in usage_rows), 6),
        total_tokens=sum(
            u.input_tokens + u.output_tokens + u.cache_read_tokens + u.cache_creation_tokens
            for u in usage_rows
        ),
    )


@router.get("/{task_id}/trace", response_model=TaskTrace)
async def get_trace(task_id: int, session: AsyncSession = Depends(get_db)) -> TaskTrace:
    """The full run, as one list ordered in real time.

    Messages, tool calls and usage rows interleave here rather than sitting in
    three separate lists — reconstructing what actually happened means seeing
    them in the order they occurred.
    """
    task = await _load_task(session, task_id)
    agent_key = await _agent_key_for(session, task)

    entries: list[TraceEntry] = []

    for m in (
        (await session.execute(select(Message).where(Message.task_id == task_id))).scalars().all()
    ):
        entries.append(
            TraceEntry(
                kind="message",
                at=m.created_at,
                iteration=m.iteration,
                ref_id=m.id,
                summary=f"{m.message_type} {m.from_agent or 'human'} → {m.to_agent or 'human'}",
                detail={"role": m.role, "message_type": m.message_type, "content": m.content},
            )
        )

    for c in (
        (await session.execute(select(ToolCall).where(ToolCall.task_id == task_id))).scalars().all()
    ):
        entries.append(
            TraceEntry(
                kind="tool_call",
                at=c.created_at,
                iteration=0,
                ref_id=c.id,
                summary=f"{c.tool_name} ({c.duration_ms or 0}ms)" + (" [error]" if c.error else ""),
                detail={
                    "tool_name": c.tool_name,
                    "arguments": c.arguments,
                    "result": c.result,
                    "duration_ms": c.duration_ms,
                    "error": c.error,
                },
            )
        )

    usage_rows = (
        (await session.execute(select(Usage).where(Usage.task_id == task_id))).scalars().all()
    )
    for u in usage_rows:
        entries.append(
            TraceEntry(
                kind="usage",
                at=u.created_at,
                iteration=u.iteration,
                ref_id=u.id,
                summary=f"{u.model}: in {u.input_tokens} / out {u.output_tokens} "
                f"(${u.estimated_cost_usd:.4f})",
                detail={
                    "model": u.model,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "estimated_cost_usd": u.estimated_cost_usd,
                },
            )
        )

    # Sort by time, then by id within a timestamp: SQLite's CURRENT_TIMESTAMP
    # has one-second resolution, so several rows in one iteration share an
    # instant and would otherwise come back in arbitrary order.
    entries.sort(key=lambda e: (e.at, e.ref_id))

    tree, delegation = await _tree_rollup(session, task)

    return TaskTrace(
        task_id=task.id,
        status=task.status,
        agent_key=agent_key,
        attempt=task.attempt,
        entries=entries,
        tree=tree,
        delegation=delegation,
        total_cost_usd=round(sum(u.estimated_cost_usd for u in usage_rows), 6),
        total_tokens=sum(
            u.input_tokens + u.output_tokens + u.cache_read_tokens + u.cache_creation_tokens
            for u in usage_rows
        ),
    )


@router.post("/{task_id}/review", response_model=TaskSummary)
async def review_task(
    task_id: int, payload: ReviewRequest, session: AsyncSession = Depends(get_db)
) -> TaskSummary:
    """Approve or reject completed work.

    Approving finishes the task. Rejecting appends the feedback to the
    conversation as a new user message and re-queues the task, so the agent
    resumes with the criticism in context rather than starting over blind.
    """
    task = await _load_task(session, task_id)
    agent_key = await _agent_key_for(session, task)

    if task.status != TaskStatus.NEEDS_REVIEW:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"task {task_id} is {task.status!r}, not {TaskStatus.NEEDS_REVIEW!r}; "
            "only work awaiting review can be reviewed",
        )

    target = TaskStatus.DONE if payload.decision == "approve" else TaskStatus.QUEUED

    if payload.decision == "reject" and not payload.feedback:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "feedback is required when rejecting: it becomes the agent's next instruction",
        )

    try:
        validate_transition(task.status, target)
    except IllegalTransitionError as exc:  # pragma: no cover - guarded above
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    previous = task.status
    task.status = target
    task.reviewed_at = datetime.now(UTC)
    bus = get_event_bus()

    if payload.decision == "reject":
        task.review_feedback = payload.feedback
        task.attempt += 1
        message = Message(
            task_id=task.id,
            from_agent=None,
            to_agent=agent_key,
            role="user",
            content=payload.feedback,
            message_type=MessageType.USER,
            iteration=0,
        )
        session.add(message)
        await session.flush()
        await bus.publish(
            MessageCreated(
                task_id=task.id,
                message_id=message.id,
                message_type=MessageType.USER,
                role="user",
                from_agent=None,
                to_agent=agent_key,
                preview=payload.feedback,
            )
        )

    await session.flush()
    await bus.publish(
        TaskStatusChanged(
            task_id=task.id,
            agent_key=agent_key,
            status=task.status,
            previous_status=previous,
            attempt=task.attempt,
        )
    )
    await session.commit()
    # updated_at carries onupdate=func.now(), so SQLAlchemy invalidates it after
    # the UPDATE regardless of expire_on_commit. Building the response would
    # then trigger a lazy load outside greenlet context and raise
    # MissingGreenlet. Refresh explicitly, inside the async context.
    await session.refresh(task)

    if payload.decision == "reject":
        await get_worker_pool().submit(task.id, agent_key)

    return _summary(task, agent_key)
