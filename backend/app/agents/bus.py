"""Inter-agent message bus.

Routes messages between agents, persists every one, and enforces the limits
that keep a delegation tree from running away.

Two delivery modes, both going through here so that nothing crosses between
agents without a persisted record:

  notify — fire and forget. The sender records what it told the recipient and
           carries on. The recipient sees it the next time it starts a task.
  ask    — blocking. The caller's loop pauses, the target runs a child task,
           and its answer comes back as the caller's tool result.

Not to be confused with `app.events.bus`, which fans typed events out to
WebSocket subscribers. This one moves work between agents; that one tells the
UI what happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.config_loader import AgentConfigError, get_agent_config
from app.agents.delegation import DelegationContext, DelegationError
from app.core.logging import get_logger
from app.events.bus import EventBus
from app.events.schemas import MessageCreated, TaskCreated
from app.models import Agent, Message, MessageType, Task, TaskStatus

if TYPE_CHECKING:  # pragma: no cover
    from app.agents.runtime import RunResult

log = get_logger(__name__)

# A child task's answer is the agent's final text. Beyond this it is truncated:
# the caller needs the answer, not the whole transcript, and an unbounded
# result would blow the caller's own token budget.
MAX_ANSWER_CHARS = 8_000


class UnknownRecipientError(ValueError):
    """Raised when an agent addresses a teammate that is not on the roster."""


@dataclass
class Delivery:
    """Outcome of one inter-agent message."""

    delivered: bool
    detail: str
    message_id: int | None = None
    child_task_id: int | None = None
    answer: str | None = None


class AgentMessageBus:
    """Routes and records messages between agents."""

    def __init__(
        self,
        session: AsyncSession,
        events: EventBus | None = None,
    ) -> None:
        self.session = session
        self.events = events

    # -- helpers -----------------------------------------------------------

    async def _emit(self, event) -> None:
        if self.events is not None:
            await self.events.publish(event)

    def _resolve(self, agent_key: str, sender: str) -> None:
        """Confirm the recipient exists, with a message naming who is available.

        An agent that guesses a teammate's key should be told the roster rather
        than left to retry variations of the same wrong name.
        """
        try:
            get_agent_config(agent_key)
        except AgentConfigError as exc:
            raise UnknownRecipientError(
                f"there is no agent {agent_key!r} on this team. {exc}"
            ) from exc
        if agent_key == sender:
            raise UnknownRecipientError(
                f"{sender!r} cannot send a message to itself. Do the work, or "
                "explain what is blocking you."
            )

    async def _record(
        self,
        task_id: int,
        from_agent: str,
        to_agent: str,
        content: str,
        iteration: int,
        is_reply: bool = False,
    ) -> Message:
        """Persist one crossing between agents.

        `role` doubles as the discriminator between an initiating message and a
        reply. A hop is a crossing the sender chose to make; the answer coming
        back is the return leg of that same hop, not a second one. Without the
        distinction, a context rebuilt from the database counts every ask twice
        and halves the effective hop budget.
        """
        row = Message(
            task_id=task_id,
            from_agent=from_agent,
            to_agent=to_agent,
            role="user" if is_reply else "assistant",
            content=content,
            message_type=MessageType.AGENT_TO_AGENT,
            iteration=iteration,
        )
        self.session.add(row)
        await self.session.flush()
        message_id = row.id
        await self.session.commit()

        await self._emit(
            MessageCreated(
                task_id=task_id,
                message_id=message_id,
                message_type=MessageType.AGENT_TO_AGENT,
                role="user" if is_reply else "assistant",
                from_agent=from_agent,
                to_agent=to_agent,
                iteration=iteration,
                preview=content[:240],
            )
        )
        return row

    # -- notify ------------------------------------------------------------

    async def notify(
        self,
        context: DelegationContext,
        from_agent: str,
        to_agent: str,
        content: str,
        task_id: int,
        parent_task: Task | None = None,
        iteration: int = 0,
        wake: Any = None,
    ) -> Delivery:
        """Fire-and-forget, but not inert: the recipient is woken to act on it.

        The sender does not wait — that is what makes it fire-and-forget — but
        the message does not sit unread until the recipient happens to get
        another task. A follow-up task is created for them and queued, so a
        notification actually reaches someone who is idle.

        A notification costs a hop like any other crossing. Letting it be free
        would give an easy way to spam the team past every limit, and now that
        each one can start work, an unbounded notification is an unbounded
        amount of work.
        """
        self._resolve(to_agent, from_agent)
        context.authorise(to_agent)
        context.counters.hops += 1

        row = await self._record(task_id, from_agent, to_agent, content, iteration)
        log.info("[%s → %s] notify (task %s)", from_agent, to_agent, task_id)

        woken_task_id: int | None = None
        if wake is not None and parent_task is not None:
            woken_task_id = await self._wake(
                from_agent=from_agent,
                to_agent=to_agent,
                content=content,
                parent_task=parent_task,
                wake=wake,
            )

        if woken_task_id is not None:
            detail = (
                f"Message delivered to {to_agent}, and they have been given a follow-up "
                f"task (#{woken_task_id}) to act on it. You are not waiting for them — "
                "continue with your own work."
            )
        else:
            detail = (
                f"Message delivered to {to_agent}. It is recorded in the trace and will be "
                f"visible to {to_agent} on its next task. Nothing is waiting on a reply."
            )

        return Delivery(
            delivered=True,
            detail=detail,
            message_id=row.id,
            child_task_id=woken_task_id,
        )

    async def _wake(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        parent_task: Task,
        wake: Any,
    ) -> int | None:
        """Queue a follow-up task so an idle recipient acts on a notification.

        Deliberately framed as a notification rather than an instruction. An
        agent handed a bare message treats it as an order and invents work from
        an FYI; telling it that acknowledging and stopping is a valid outcome is
        what keeps 'the contract is published' from becoming a second project.
        """
        agent_row = (
            await self.session.execute(select(Agent).where(Agent.key == to_agent))
        ).scalar_one_or_none()
        if agent_row is None:
            from app.agents.runtime import sync_agent_row

            agent_row = await sync_agent_row(self.session, get_agent_config(to_agent))

        description = (
            f"{from_agent} sent you this message:\n\n"
            f"---\n{content}\n---\n\n"
            "Decide whether it needs anything from you. If it does, do that work and "
            "say what you did. If it is only something to be aware of — a contract "
            "published, an assumption changed — acknowledge it in one line and stop. "
            "Do not invent work that was not asked for."
        )

        follow_up = Task(
            title=f"[{from_agent} → {to_agent}] {content[:80]}",
            description=description,
            assigned_agent_id=agent_row.id,
            created_by=from_agent,
            parent_task_id=parent_task.id,
            status=TaskStatus.QUEUED,
        )
        self.session.add(follow_up)
        await self.session.flush()
        follow_up_id = follow_up.id
        await self.session.commit()

        await self._emit(
            TaskCreated(
                task_id=follow_up_id,
                agent_key=to_agent,
                title=follow_up.title,
                status=follow_up.status,
                created_by=from_agent,
            )
        )

        # Queued, not run inline: the sender is explicitly not waiting, and
        # running it here would make a fire-and-forget call blocking.
        await wake(follow_up_id, to_agent)
        log.info("[%s → %s] woke recipient with task %s", from_agent, to_agent, follow_up_id)
        return follow_up_id

    # -- ask ---------------------------------------------------------------

    async def ask(
        self,
        context: DelegationContext,
        from_agent: str,
        to_agent: str,
        question: str,
        parent_task: Task,
        iteration: int,
        run_child,
    ) -> Delivery:
        """Blocking request: create a child task, run it, return its answer.

        `run_child` is injected rather than imported so this module does not
        depend on the runtime that depends on it.
        """
        self._resolve(to_agent, from_agent)
        context.authorise(to_agent)

        await self._record(context.root_task_id, from_agent, to_agent, question, iteration)

        agent_row = (
            await self.session.execute(select(Agent).where(Agent.key == to_agent))
        ).scalar_one_or_none()
        if agent_row is None:
            from app.agents.runtime import sync_agent_row

            agent_row = await sync_agent_row(self.session, get_agent_config(to_agent))

        child = Task(
            title=f"[{from_agent} → {to_agent}] {question[:90]}",
            description=question,
            assigned_agent_id=agent_row.id,
            created_by=from_agent,
            parent_task_id=parent_task.id,
            status=TaskStatus.QUEUED,
        )
        self.session.add(child)
        await self.session.flush()
        child_id = child.id
        await self.session.commit()

        await self._emit(
            TaskCreated(
                task_id=child_id,
                agent_key=to_agent,
                title=child.title,
                status=child.status,
                created_by=from_agent,
            )
        )

        child_context = context.descend(to_agent)
        log.info(
            "[%s → %s] ask (child task %s, hop %d/%d)",
            from_agent,
            to_agent,
            child_id,
            context.hops,
            context.max_hops,
        )

        result: RunResult = await run_child(to_agent, child, child_context)

        answer = (result.final_text or "").strip()
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[:MAX_ANSWER_CHARS] + "\n\n[answer truncated]"

        if not answer:
            answer = (
                f"{to_agent} finished with status {result.status} and produced no written "
                "answer. Proceed without it, or say what you still need."
            )

        # The reply is recorded in the direction it travelled, so the trace
        # reads as a conversation rather than a list of one-way sends.
        await self._record(
            context.root_task_id, to_agent, from_agent, answer, iteration, is_reply=True
        )

        return Delivery(
            delivered=True,
            detail=answer,
            child_task_id=child_id,
            answer=answer,
        )


__all__ = [
    "AgentMessageBus",
    "DelegationError",
    "Delivery",
    "UnknownRecipientError",
]
