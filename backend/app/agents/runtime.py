"""Generic agent runtime.

One class runs any agent. Everything that distinguishes agents — name, model,
prompt, tool grants, budgets — is read from config; this module contains no
knowledge of any particular agent, which is what makes swapping the roster a
config change rather than a rewrite.

The loop is written by hand rather than using the SDK's beta tool runner
because every step has to be persisted as it happens and budgets have to be
checked between iterations — neither of which the runner exposes.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.budget import BudgetExceeded, BudgetTracker
from app.agents.bus import AgentMessageBus
from app.agents.config_loader import AgentConfig, get_agent_config
from app.agents.delegation import (
    DelegationContext,
    DelegationTimeout,
    new_root_context,
    restore_context,
)
from app.agents.lifecycle import validate_transition
from app.agents.project_context import context_prelude, ensure_exists
from app.agents.tools import build_toolset
from app.agents.tools.base import Tool, ToolOutcome
from app.agents.tools.collaboration import ToolContext
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.pricing import TokenUsage, UnknownModelError, estimate_cost_usd, rates_for
from app.events.bus import EventBus
from app.events.schemas import (
    AgentStatusChanged,
    MessageCreated,
    TaskStatusChanged,
    ToolFinished,
    ToolStarted,
    UsageUpdated,
)
from app.models import Agent, AgentStatus, Message, MessageType, Task, TaskStatus, ToolCall, Usage

log = get_logger(__name__)

# stop_reason values that end the loop with a completed task.
TERMINAL_STOP_REASONS = {"end_turn", "stop_sequence"}
MAX_PAUSE_RESUMES = 5


class AgentRuntimeError(RuntimeError):
    """Raised when a run cannot proceed for a reason that is not a budget trip."""


@dataclass
class RunResult:
    """Outcome of one task run."""

    task_id: int
    agent_key: str
    status: str
    final_text: str
    halt_reason: str | None
    budget: dict[str, float | int]

    @property
    def ok(self) -> bool:
        return self.status in {TaskStatus.NEEDS_REVIEW, TaskStatus.DONE}


PREVIEW_CHARS = 240


def _preview(value: Any) -> str:
    """A short, readable excerpt for the event stream.

    The WebSocket is a notification channel; clients fetch full bodies over
    REST. Sending whole file contents through every subscriber's queue would
    make a large write_file stall the stream for everyone.

    Content blocks are summarised rather than stringified. A raw dump of
    Messages API blocks is unreadable in a live view — it is mostly ids and
    punctuation — and the live view is the thing a person actually watches.
    """
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts: list[str] = []
        for block in value:
            data = block if isinstance(block, dict) else _serialize(block)
            if not isinstance(data, dict):
                parts.append(str(data))
                continue
            kind = data.get("type")
            if kind == "text":
                parts.append(str(data.get("text", "")).strip())
            elif kind == "tool_use":
                parts.append(f"→ {data.get('name')}")
            elif kind == "tool_result":
                body = data.get("content")
                prefix = "✗ " if data.get("is_error") else ""
                parts.append(f"{prefix}{body if isinstance(body, str) else ''}".strip())
            elif kind == "thinking":
                parts.append("(thinking)")
        text = "  ".join(p for p in parts if p)
    elif isinstance(value, dict):
        # Tool arguments: show the values, which are what identify the call.
        text = "  ".join(f"{k}={v}" for k, v in value.items())
    else:
        text = str(value)

    text = " ".join(text.split())
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


def _serialize(blocks: Any) -> Any:
    """Convert SDK content blocks to JSON-safe structures for persistence."""
    if isinstance(blocks, list):
        return [_serialize(b) for b in blocks]
    # Guard on the method actually called. SDK pydantic models expose both
    # model_dump and model_dump_json; testing one and calling the other would
    # break on any object that has only the former.
    if hasattr(blocks, "model_dump_json"):
        return json.loads(blocks.model_dump_json())
    if hasattr(blocks, "model_dump"):
        return _serialize(blocks.model_dump())
    if isinstance(blocks, dict):
        return {k: _serialize(v) for k, v in blocks.items()}
    return blocks


class AgentRuntime:
    """Runs one agent against one task."""

    def __init__(
        self,
        config: AgentConfig,
        session: AsyncSession,
        settings: Settings | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        bus: EventBus | None = None,
        delegation: DelegationContext | None = None,
        client_for: Any = None,
        wake: Any = None,
    ) -> None:
        self.config = config
        self.session = session
        self.settings = settings or get_settings()
        self._client = client
        # Optional so the runtime stays usable from the CLI and from tests that
        # do not care about streaming.
        self.bus = bus
        self._agent_status = AgentStatus.IDLE
        # None on a root run; a child run inherits its parent's tree.
        self.delegation = delegation
        # How a child run gets its own model client. Without it a delegated run
        # would reuse the caller's scripted or configured client.
        self.client_for = client_for
        # Queues a follow-up task when this agent notifies a teammate. None
        # outside the worker (the CLI, most tests), where a notification is
        # still delivered and recorded but wakes nobody.
        self.wake = wake
        self.messages_bus = AgentMessageBus(session, events=bus)
        self.tool_schemas, self.tools = build_toolset(config.tools)
        self.budget = BudgetTracker(
            max_iterations=config.max_iterations or self.settings.max_iterations,
            max_tokens=config.max_tokens or self.settings.max_tokens_per_task,
            max_hops=self.settings.max_agent_hops,
        )

    # -- construction ------------------------------------------------------

    @classmethod
    def for_agent(cls, key: str, session: AsyncSession, **kwargs: Any) -> AgentRuntime:
        return cls(get_agent_config(key), session, **kwargs)

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.settings.require_api_key())
        return self._client

    # -- persistence helpers ----------------------------------------------

    async def _build_conversation(self, task: Task) -> list[dict[str, Any]]:
        """Assemble the message list to send to the model.

        A first attempt is just the task description. A re-queued task — one the
        manager rejected — has to resume with everything it already did plus the
        criticism, or the agent re-reads a workspace it does not remember
        writing and the feedback lands with no context.

        Rebuilt from the persisted rows rather than kept in memory: the worker
        that picks up attempt 2 is not the process that ran attempt 1.
        """
        if task.attempt <= 1:
            prelude = context_prelude(self.settings)
            return [
                {
                    "role": "user",
                    "content": f"{prelude}\n\n---\n\n## Your task\n\n{task.description}",
                }
            ]

        rows = (
            (
                await self.session.execute(
                    select(Message).where(Message.task_id == task.id).order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )

        # Rows that exist for the trace but must not be replayed to the model.
        #
        # TOOL_USE duplicates blocks already inside the ASSISTANT row for the
        # same turn. AGENT_TO_AGENT is a side-channel record of a delegation the
        # model already saw as a tool_use/tool_result pair — replaying it would
        # tell the agent that the *manager* asked its own question, duplicate
        # the teammate's answer, and wedge both between a tool_use and the
        # tool_result that must follow it.
        SIDE_CHANNEL = {MessageType.TOOL_USE, MessageType.AGENT_TO_AGENT}

        conversation: list[dict[str, Any]] = []
        for row in rows:
            if row.message_type in SIDE_CHANNEL:
                continue
            role = "assistant" if row.message_type == MessageType.ASSISTANT else "user"
            conversation.append({"role": role, "content": row.content})

        if not conversation:  # pragma: no cover - defensive
            return [{"role": "user", "content": task.description}]

        # The API requires the last turn to be from the user. A rejection always
        # appends the manager's feedback, so this normally already holds.
        if conversation[-1]["role"] != "user":
            conversation.append(
                {
                    "role": "user",
                    "content": task.review_feedback or "Please revise your previous work.",
                }
            )
        return conversation

    async def _checkpoint(self) -> None:
        """Commit the work so far.

        Two reasons this is a commit and not a flush. SQLite allows a single
        writer, so holding one transaction open for an entire agent run blocks
        every other agent — the worker pool's concurrency is fictional without
        this. And a trace that only becomes visible after the run finishes is
        useless for watching a run in progress, which is the point of the
        product.

        Committing as we go also means a crash leaves the trace of what
        happened rather than rolling it away, which is what you want from an
        audit trail.
        """
        await self.session.commit()

    async def _emit(self, event) -> None:
        if self.bus is not None:
            await self.bus.publish(event)

    async def set_agent_status(self, status: str, task_id: int | None = None) -> None:
        """Update the agent's live status and announce it.

        Distinct from task status: the agent is what the UI animates, and it
        goes idle between tasks while the task itself may sit in needs_review.
        """
        if status == self._agent_status:
            return
        previous, self._agent_status = self._agent_status, status

        row = (
            await self.session.execute(select(Agent).where(Agent.key == self.config.key))
        ).scalar_one_or_none()
        if row is not None:
            row.status = status
            row.status_changed_at = datetime.now(UTC)
            await self.session.flush()

        await self._emit(
            AgentStatusChanged(
                agent_key=self.config.key,
                status=status,
                previous_status=previous,
                task_id=task_id,
            )
        )

    async def _persist_message(
        self,
        task_id: int,
        role: str,
        content: Any,
        message_type: str,
        from_agent: str | None,
        to_agent: str | None,
        iteration: int,
    ) -> None:
        row = Message(
            task_id=task_id,
            from_agent=from_agent,
            to_agent=to_agent,
            role=role,
            content=_serialize(content),
            message_type=message_type,
            iteration=iteration,
        )
        self.session.add(row)
        await self.session.flush()
        message_id = row.id
        await self._checkpoint()
        await self._emit(
            MessageCreated(
                task_id=task_id,
                message_id=message_id,
                message_type=message_type,
                role=role,
                from_agent=from_agent,
                to_agent=to_agent,
                iteration=iteration,
                preview=_preview(content),
            )
        )

    async def _persist_usage(self, task_id: int, usage: TokenUsage, cost: float, it: int) -> None:
        self.session.add(
            Usage(
                task_id=task_id,
                agent_key=self.config.key,
                model=self.config.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                estimated_cost_usd=cost,
                iteration=it,
            )
        )
        await self._checkpoint()
        await self._emit(
            UsageUpdated(
                task_id=task_id,
                agent_key=self.config.key,
                model=self.config.model,
                iteration=it,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.billable_total,
                estimated_cost_usd=round(cost, 6),
                task_total_cost_usd=round(self.budget.cost_usd, 6),
            )
        )

    async def _set_status(self, task: Task, status: str, halt_reason: str | None = None) -> None:
        # Validated centrally: a task that jumps states illegally has a history
        # that cannot be reconstructed, which defeats the point of the trace.
        validate_transition(task.status, status)
        previous, task.status = task.status, status
        if halt_reason is not None:
            task.halt_reason = halt_reason
        await self._checkpoint()
        await self._emit(
            TaskStatusChanged(
                task_id=task.id,
                agent_key=self.config.key,
                status=status,
                previous_status=previous,
                halt_reason=halt_reason,
                attempt=task.attempt,
            )
        )

    # -- tool execution ----------------------------------------------------

    def _tool_context(self, task: Task, iteration: int) -> ToolContext:
        return ToolContext(
            agent_key=self.config.key,
            task=task,
            delegation=self.delegation,
            bus=self.messages_bus,
            iteration=iteration,
            settings=self.settings,
            run_child=self._run_child,
            wake=self.wake if self.settings.wake_on_notify else None,
        )

    async def _run_child(self, agent_key: str, child_task: Task, child_context: DelegationContext):
        """Run a delegated task to completion and return its result.

        Executed inline rather than handed to the worker queue. The caller is
        blocked either way, and queuing would risk a deadlock: the target's lane
        may already be occupied by a task that is itself waiting on this one.

        The child shares the caller's session — the two runs are strictly
        sequential, so there is no concurrent-flush hazard, and sharing means the
        child's rows land in the same trace without a second connection.
        """
        child_config = get_agent_config(agent_key)
        child_runtime = AgentRuntime(
            child_config,
            self.session,
            self.settings,
            client=self.client_for(agent_key) if self.client_for else None,
            bus=self.bus,
            delegation=child_context,
            client_for=self.client_for,
            wake=self.wake,
        )
        return await child_runtime.run(child_task)

    async def _invoke_tool(
        self, block: Any, context: ToolContext | None = None
    ) -> tuple[ToolOutcome, int]:
        """Run one tool handler.

        Filesystem tools touch no session state and are safe to run
        concurrently. Collaboration tools do touch it, so the caller runs
        those one at a time."""
        name = block.name
        # Inputs are already parsed dicts from the SDK; never string-match the
        # serialized form, which varies in escaping between models.
        arguments: dict[str, Any] = dict(block.input or {})
        tool: Tool | None = self.tools.get(name)
        started = time.perf_counter()

        if tool is None:
            outcome = ToolOutcome(
                content=(
                    f"Tool {name!r} is not available to you. "
                    f"Available tools: {sorted(self.tools)}."
                ),
                payload={"unavailable_tool": name},
                is_error=True,
            )
        else:
            try:
                if tool.needs_context:
                    outcome = await tool.handler(context, **arguments)
                else:
                    outcome = await tool.handler(**arguments)
            except TypeError as exc:
                outcome = ToolOutcome(
                    content=f"Invalid arguments for {name}: {exc}",
                    payload={"arguments": arguments},
                    is_error=True,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
                log.exception("tool %s raised", name)
                outcome = ToolOutcome(
                    content=f"{name} failed with {type(exc).__name__}: {exc}",
                    payload={"exception": type(exc).__name__},
                    is_error=True,
                )

        return outcome, int((time.perf_counter() - started) * 1000)

    async def _run_tool_batch(
        self, task_id: int, blocks: list[Any], context: ToolContext | None = None
    ) -> list[dict[str, Any]]:
        """Execute one turn's tool calls and return their tool_result blocks.

        Three phases, because an AsyncSession is not concurrency-safe: rows are
        written before execution (spec section 3), the handlers then run
        concurrently with no session access, and results are written back
        sequentially afterwards. Interleaving session.add()/flush() across
        gathered coroutines corrupts the unit of work.
        """
        records: list[ToolCall] = []
        for block in blocks:
            record = ToolCall(
                task_id=task_id,
                agent_key=self.config.key,
                tool_name=block.name,
                tool_use_id=block.id,
                arguments=dict(block.input or {}),
            )
            self.session.add(record)
            records.append(record)
        # Committed before the tools run, not merely flushed: spec section 3
        # requires the row to exist before execution, and an uncommitted row
        # would vanish if the process died mid-command.
        await self._checkpoint()

        for block, record in zip(blocks, records, strict=True):
            await self._emit(
                ToolStarted(
                    task_id=task_id,
                    agent_key=self.config.key,
                    tool_call_id=record.id,
                    tool_name=block.name,
                    arguments_preview=_preview(dict(block.input or {})),
                )
            )

        # Collaboration tools write to the session and can start a nested run;
        # an AsyncSession is not concurrency-safe, so a batch containing one is
        # executed in order. Pure filesystem batches still run concurrently.
        uses_session = any(
            (tool := self.tools.get(b.name)) is not None and tool.needs_context for b in blocks
        )
        if uses_session:
            outcomes = [await self._invoke_tool(b, context) for b in blocks]
        else:
            outcomes = await asyncio.gather(*(self._invoke_tool(b, context) for b in blocks))

        results: list[dict[str, Any]] = []
        for block, record, (outcome, duration_ms) in zip(blocks, records, outcomes, strict=True):
            record.result = outcome.payload
            record.duration_ms = duration_ms
            record.error = outcome.content if outcome.is_error else None
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": outcome.content,
                    "is_error": outcome.is_error,
                }
            )
            await self._emit(
                ToolFinished(
                    task_id=task_id,
                    agent_key=self.config.key,
                    tool_call_id=record.id,
                    tool_name=block.name,
                    duration_ms=duration_ms,
                    is_error=outcome.is_error,
                    error=outcome.content if outcome.is_error else None,
                )
            )
        await self._checkpoint()
        return results

    # -- API call ----------------------------------------------------------

    def _request_kwargs(self, messages: list[dict[str, Any]], system: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.settings.max_response_tokens,
            "system": system,
            "messages": messages,
            "tools": self.tool_schemas,
        }
        # `thinking` is deliberately omitted: every current model runs its own
        # default (adaptive on Opus 5 and Sonnet 5), and sending an explicit
        # config would break the moment a different model is configured.
        effort = self.config.effort or self.settings.effort
        if effort != "off":
            kwargs["output_config"] = {"effort": effort}
        return kwargs

    async def _call_api(self, messages: list[dict[str, Any]], system: str) -> Any:
        try:
            return await self.client.messages.create(**self._request_kwargs(messages, system))
        except anthropic.NotFoundError as exc:
            raise AgentRuntimeError(
                f"model {self.config.model!r} was not found. Check the `model` field in "
                f"config/agents/{self.config.key}.yaml."
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise AgentRuntimeError(
                "ANTHROPIC_API_KEY was rejected. Check the key in your environment."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise AgentRuntimeError(
                "rate limited by the API after the SDK's own retries; try again shortly."
            ) from exc
        except anthropic.APIStatusError as exc:
            raise AgentRuntimeError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AgentRuntimeError(f"could not reach the API: {exc}") from exc

    # -- the loop ----------------------------------------------------------

    async def run(self, task: Task) -> RunResult:
        """Run the tool-use loop until a final response or a budget trip."""
        system = self.config.resolve_system_prompt(self.settings.agent_configs)

        # A root run starts a new tree; a delegated run inherits its caller's,
        # so the hop budget and the clock are shared rather than reset.
        if self.delegation is None:
            if task.parent_task_id is None and task.attempt <= 1:
                # A fresh root task: nothing to restore.
                self.delegation = new_root_context(
                    root_task_id=task.id,
                    max_hops=self.settings.max_agent_hops,
                    deadline_seconds=self.settings.task_deadline_seconds,
                    agent_key=self.config.key,
                )
            else:
                # A task the worker picked up without a live parent — a woken
                # notification, or a re-queued attempt. Rebuilding from the
                # database keeps one budget across the tree even though the
                # in-memory context is gone.
                self.delegation = await restore_context(
                    self.session,
                    task,
                    max_hops=self.settings.max_agent_hops,
                    deadline_seconds=self.settings.task_deadline_seconds,
                    agent_key=self.config.key,
                )

        ensure_exists(self.settings)
        messages = await self._build_conversation(task)

        # Pre-flight: a model with no rate entry cannot be costed, and finding
        # that out mid-loop would strand the task after real spend. Check it
        # before the first API call.
        try:
            rates_for(self.config.model)
        except UnknownModelError as exc:
            reason = str(exc).strip('"')
            await self._set_status(task, TaskStatus.FAILED, reason)
            await self.set_agent_status(AgentStatus.ERROR, task.id)
            return self._result(task, TaskStatus.FAILED, "", reason)

        await self._set_status(task, TaskStatus.IN_PROGRESS)
        await self.set_agent_status(AgentStatus.THINKING, task.id)
        await self._persist_message(
            task.id, "user", task.description, MessageType.USER, None, self.config.key, 0
        )

        final_text = ""
        pause_resumes = 0

        try:
            while True:
                self.budget.check_before_iteration()
                # One clock for the whole tree. Checked here as well as at each
                # delegation, so a single agent looping on its own tools is
                # bounded too.
                self.delegation.check_deadline()
                iteration = self.budget.record_iteration()

                log.info(
                    "[%s] iteration %d/%d (%d tokens, $%.4f so far)",
                    self.config.key,
                    iteration,
                    self.budget.max_iterations,
                    self.budget.total_tokens,
                    self.budget.cost_usd,
                )

                await self.set_agent_status(AgentStatus.THINKING, task.id)
                response = await self._call_api(messages, system)

                usage = TokenUsage.from_response_usage(response.usage)
                cost = estimate_cost_usd(self.config.model, usage)
                self.budget.record_usage(usage, cost)
                await self._persist_usage(task.id, usage, cost, iteration)

                await self._persist_message(
                    task.id,
                    "assistant",
                    response.content,
                    MessageType.ASSISTANT,
                    self.config.key,
                    None,
                    iteration,
                )

                text = "".join(b.text for b in response.content if b.type == "text")

                if response.stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    reason = (
                        f"the model declined this request "
                        f"(category: {getattr(details, 'category', 'unknown')})"
                    )
                    await self._set_status(task, TaskStatus.FAILED, reason)
                    await self.set_agent_status(AgentStatus.ERROR, task.id)
                    return self._result(task, TaskStatus.FAILED, text, reason)

                if response.stop_reason == "max_tokens":
                    reason = (
                        f"the response hit the {self.settings.max_response_tokens} token "
                        "response cap and was truncated mid-output"
                    )
                    await self._set_status(task, TaskStatus.FAILED, reason)
                    await self.set_agent_status(AgentStatus.ERROR, task.id)
                    return self._result(task, TaskStatus.FAILED, text, reason)

                if response.stop_reason == "pause_turn":
                    # A server-side tool paused the turn; resend to continue.
                    pause_resumes += 1
                    if pause_resumes > MAX_PAUSE_RESUMES:
                        reason = f"turn still paused after {MAX_PAUSE_RESUMES} resumes"
                        await self._set_status(task, TaskStatus.FAILED, reason)
                        return self._result(task, TaskStatus.FAILED, text, reason)
                    messages.append({"role": "assistant", "content": response.content})
                    continue

                if response.stop_reason in TERMINAL_STOP_REASONS:
                    final_text = text
                    await self._set_status(task, TaskStatus.NEEDS_REVIEW)
                    # The work is done; the manager now owes a decision.
                    await self.set_agent_status(AgentStatus.WAITING_ON_HUMAN, task.id)
                    log.info("[%s] awaiting review after %d iterations", self.config.key, iteration)
                    return self._result(task, TaskStatus.NEEDS_REVIEW, final_text, None)

                # stop_reason == "tool_use"
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    reason = f"unexpected stop_reason {response.stop_reason!r} with no tool calls"
                    await self._set_status(task, TaskStatus.FAILED, reason)
                    await self.set_agent_status(AgentStatus.ERROR, task.id)
                    return self._result(task, TaskStatus.FAILED, text, reason)

                # Echo the assistant turn back verbatim — including thinking
                # blocks, which must be replayed unchanged on the same model.
                messages.append({"role": "assistant", "content": response.content})

                await self._persist_message(
                    task.id,
                    "assistant",
                    [_serialize(b) for b in tool_uses],
                    MessageType.TOOL_USE,
                    self.config.key,
                    None,
                    iteration,
                )

                # Parallel tool calls run concurrently and every result goes
                # back in ONE user message; splitting them teaches the model to
                # stop calling tools in parallel.
                await self.set_agent_status(AgentStatus.WORKING, task.id)
                results = await self._run_tool_batch(
                    task.id, tool_uses, self._tool_context(task, iteration)
                )

                await self._persist_message(
                    task.id,
                    "user",
                    results,
                    MessageType.TOOL_RESULT,
                    None,
                    self.config.key,
                    iteration,
                )
                messages.append({"role": "user", "content": results})

        except DelegationTimeout as exc:
            log.warning("[%s] %s", self.config.key, exc)
            await self._set_status(task, TaskStatus.BUDGET_EXCEEDED, str(exc))
            await self.set_agent_status(AgentStatus.BLOCKED, task.id)
            return self._result(task, TaskStatus.BUDGET_EXCEEDED, final_text, str(exc))
        except BudgetExceeded as exc:
            log.warning("[%s] %s", self.config.key, exc)
            await self._set_status(task, TaskStatus.BUDGET_EXCEEDED, str(exc))
            # Blocked by a ceiling, not broken — distinct from an error.
            await self.set_agent_status(AgentStatus.BLOCKED, task.id)
            return self._result(task, TaskStatus.BUDGET_EXCEEDED, final_text, str(exc))
        except AgentRuntimeError as exc:
            log.error("[%s] %s", self.config.key, exc)
            await self._set_status(task, TaskStatus.FAILED, str(exc))
            await self.set_agent_status(AgentStatus.ERROR, task.id)
            return self._result(task, TaskStatus.FAILED, final_text, str(exc))
        except Exception as exc:  # noqa: BLE001 - closes the task out, logged in full
            # Anything unforeseen still has to close the task out. Leaving it
            # in RUNNING strands it: no status, no reason, and the trace stops
            # mid-run with nothing saying why. The traceback is logged in full
            # rather than swallowed, so the underlying bug stays visible.
            log.exception("[%s] unexpected failure", self.config.key)
            reason = f"unexpected {type(exc).__name__}: {exc}"
            await self._set_status(task, TaskStatus.FAILED, reason)
            await self.set_agent_status(AgentStatus.ERROR, task.id)
            return self._result(task, TaskStatus.FAILED, final_text, reason)

    def _result(self, task: Task, status: str, text: str, reason: str | None) -> RunResult:
        return RunResult(
            task_id=task.id,
            agent_key=self.config.key,
            status=status,
            final_text=text,
            halt_reason=reason,
            budget=self.budget.summary(),
        )


async def sync_agent_row(session: AsyncSession, config: AgentConfig) -> Agent:
    """Upsert the DB projection of an agent config.

    YAML is the source of truth; this keeps the row in step so tasks and usage
    can foreign-key to a stable id.
    """
    from sqlalchemy import select

    existing = (
        await session.execute(select(Agent).where(Agent.key == config.key))
    ).scalar_one_or_none()

    if existing is None:
        agent = Agent(
            key=config.key,
            display_name=config.display_name,
            role=config.role,
            model=config.model,
            avatar_id=config.avatar_id,
            system_prompt_path=config.system_prompt_file,
        )
        session.add(agent)
        await session.flush()
        return agent

    existing.display_name = config.display_name
    existing.role = config.role
    existing.model = config.model
    existing.avatar_id = config.avatar_id
    existing.system_prompt_path = config.system_prompt_file
    await session.flush()
    return existing
