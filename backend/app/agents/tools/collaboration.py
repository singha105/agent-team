"""Tools that let agents talk to each other and to the shared context.

Unlike the filesystem tools these are context-aware: they need to know which
agent is calling, on which task, and under which delegation budget. The runtime
supplies a ToolContext for exactly these.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.agents.delegation import DelegationContext, DelegationError
from app.agents.project_context import append_section
from app.agents.tools.base import Tool, ToolOutcome, register

if TYPE_CHECKING:  # pragma: no cover
    from app.agents.bus import AgentMessageBus
    from app.models import Task


@dataclass
class ToolContext:
    """What a collaboration tool needs to know about its caller."""

    agent_key: str
    task: Task
    delegation: DelegationContext
    bus: AgentMessageBus
    iteration: int = 0
    settings: Any = None
    # Injected by the runtime; runs a child task to completion.
    run_child: Any = None


async def send_message(context: ToolContext, to_agent: str, content: str) -> ToolOutcome:
    """Fire-and-forget notification."""
    if not (content or "").strip():
        return ToolOutcome(
            content="Refused: an empty message tells the recipient nothing.",
            payload={"to_agent": to_agent},
            is_error=True,
        )
    try:
        delivery = await context.bus.notify(
            context=context.delegation,
            from_agent=context.agent_key,
            to_agent=to_agent,
            content=content,
            task_id=context.task.id,
            iteration=context.iteration,
        )
    except DelegationError as exc:
        # A refused delegation is reported to the agent as a tool error so it
        # can finish without the exchange, rather than killing the run.
        return ToolOutcome(
            content=str(exc), payload={"to_agent": to_agent, "refused": True}, is_error=True
        )
    except ValueError as exc:
        return ToolOutcome(content=str(exc), payload={"to_agent": to_agent}, is_error=True)

    return ToolOutcome(
        content=delivery.detail,
        payload={"to_agent": to_agent, "message_id": delivery.message_id},
    )


async def ask_agent(context: ToolContext, to_agent: str, question: str) -> ToolOutcome:
    """Blocking request. The caller waits while the target runs a child task."""
    if not (question or "").strip():
        return ToolOutcome(
            content="Refused: ask a specific question.",
            payload={"to_agent": to_agent},
            is_error=True,
        )
    try:
        delivery = await context.bus.ask(
            context=context.delegation,
            from_agent=context.agent_key,
            to_agent=to_agent,
            question=question,
            parent_task=context.task,
            iteration=context.iteration,
            run_child=context.run_child,
        )
    except DelegationError as exc:
        return ToolOutcome(
            content=str(exc), payload={"to_agent": to_agent, "refused": True}, is_error=True
        )
    except ValueError as exc:
        return ToolOutcome(content=str(exc), payload={"to_agent": to_agent}, is_error=True)

    return ToolOutcome(
        content=f"{to_agent} replied:\n\n{delivery.answer}",
        payload={
            "to_agent": to_agent,
            "child_task_id": delivery.child_task_id,
            "answer_chars": len(delivery.answer or ""),
        },
    )


async def append_project_context(context: ToolContext, heading: str, body: str) -> ToolOutcome:
    """Publish a decision to the shared context."""
    ok, detail = append_section(
        agent_key=context.agent_key,
        heading=heading,
        body=body,
        settings=context.settings,
    )
    return ToolOutcome(
        content=detail if ok else f"Not appended: {detail}",
        payload={"heading": heading, "appended": ok},
        is_error=not ok,
    )


register(
    Tool(
        name="send_message",
        description=(
            "Send a one-way note to a teammate. Use it to tell someone something they "
            "will need — that a contract is published, that an assumption changed. "
            "Nothing waits for a reply and you get no answer back. If you need an "
            "answer before you can continue, use ask_agent instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to_agent": {
                    "type": "string",
                    "description": "The teammate's key, e.g. 'database', 'frontend', 'devops'.",
                },
                "content": {"type": "string", "description": "What you want them to know."},
            },
            "required": ["to_agent", "content"],
        },
        handler=send_message,
        needs_context=True,
    )
)

register(
    Tool(
        name="ask_agent",
        description=(
            "Ask a teammate a question and wait for their answer. They run a real "
            "sub-task and their reply comes back as this tool's result.\n\n"
            "Use this when you need something that is genuinely theirs — a schema, an "
            "API contract, a deployment constraint — instead of inventing it. Ask for "
            "exactly what you need and say what you are building, so they can answer "
            "usefully in one go.\n\n"
            "This is not free: it costs a hop against a limit shared by the whole task, "
            "and you wait while they work. Check the shared context first; if the answer "
            "is already published, use it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to_agent": {
                    "type": "string",
                    "description": "The teammate's key, e.g. 'database', 'backend', 'frontend'.",
                },
                "question": {
                    "type": "string",
                    "description": (
                        "A specific question, including the context they need to answer it."
                    ),
                },
            },
            "required": ["to_agent", "question"],
        },
        handler=ask_agent,
        needs_context=True,
    )
)

register(
    Tool(
        name="append_project_context",
        description=(
            "Publish a decision to PROJECT.md, the team's shared context. Record "
            "anything the rest of the team must build against: a schema, an API "
            "contract, a convention, a constraint.\n\n"
            "The file is append-only — you cannot edit or remove what is already there, "
            "including your own earlier entries. Add what is missing rather than "
            "restating what is published."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "heading": {
                    "type": "string",
                    "description": "Short section title, e.g. 'Books table schema'.",
                },
                "body": {
                    "type": "string",
                    "description": "The decision itself, concrete enough to build against.",
                },
            },
            "required": ["heading", "body"],
        },
        handler=append_project_context,
        needs_context=True,
    )
)
