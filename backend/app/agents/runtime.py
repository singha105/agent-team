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
from typing import Any

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.budget import BudgetExceeded, BudgetTracker
from app.agents.config_loader import AgentConfig, get_agent_config
from app.agents.tools import build_toolset
from app.agents.tools.base import Tool, ToolOutcome
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.pricing import TokenUsage, estimate_cost_usd
from app.models import Agent, Message, MessageType, Task, TaskStatus, ToolCall, Usage

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
        return self.status == TaskStatus.COMPLETED


def _serialize(blocks: Any) -> Any:
    """Convert SDK content blocks to JSON-safe structures for persistence."""
    if isinstance(blocks, list):
        return [_serialize(b) for b in blocks]
    if hasattr(blocks, "model_dump"):
        return json.loads(blocks.model_dump_json())
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
    ) -> None:
        self.config = config
        self.session = session
        self.settings = settings or get_settings()
        self._client = client
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
        self.session.add(
            Message(
                task_id=task_id,
                from_agent=from_agent,
                to_agent=to_agent,
                role=role,
                content=_serialize(content),
                message_type=message_type,
                iteration=iteration,
            )
        )
        await self.session.flush()

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
        await self.session.flush()

    async def _set_status(self, task: Task, status: str, halt_reason: str | None = None) -> None:
        task.status = status
        if halt_reason is not None:
            task.halt_reason = halt_reason
        await self.session.flush()

    # -- tool execution ----------------------------------------------------

    async def _execute_tool(self, task_id: int, block: Any) -> dict[str, Any]:
        """Run one tool_use block and return its tool_result block.

        The ToolCall row is written before the tool runs, so a command that
        never returns is still visible in the trace.
        """
        name = block.name
        # Inputs are already parsed dicts from the SDK; never string-match the
        # serialized form, which varies in escaping between models.
        arguments: dict[str, Any] = dict(block.input or {})

        record = ToolCall(
            task_id=task_id,
            agent_key=self.config.key,
            tool_name=name,
            tool_use_id=block.id,
            arguments=arguments,
        )
        self.session.add(record)
        await self.session.flush()

        started = time.perf_counter()
        tool: Tool | None = self.tools.get(name)

        if tool is None:
            # The model asked for a tool this agent was not granted.
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

        duration_ms = int((time.perf_counter() - started) * 1000)
        record.result = outcome.payload
        record.duration_ms = duration_ms
        record.error = outcome.content if outcome.is_error else None
        await self.session.flush()

        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": outcome.content,
            "is_error": outcome.is_error,
        }

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
        messages: list[dict[str, Any]] = [{"role": "user", "content": task.description}]

        await self._set_status(task, TaskStatus.RUNNING)
        await self._persist_message(
            task.id, "user", task.description, MessageType.USER, None, self.config.key, 0
        )

        final_text = ""
        pause_resumes = 0

        try:
            while True:
                self.budget.check_before_iteration()
                iteration = self.budget.record_iteration()

                log.info(
                    "[%s] iteration %d/%d (%d tokens, $%.4f so far)",
                    self.config.key, iteration, self.budget.max_iterations,
                    self.budget.total_tokens, self.budget.cost_usd,
                )

                response = await self._call_api(messages, system)

                usage = TokenUsage.from_response_usage(response.usage)
                cost = estimate_cost_usd(self.config.model, usage)
                self.budget.record_usage(usage, cost)
                await self._persist_usage(task.id, usage, cost, iteration)

                await self._persist_message(
                    task.id, "assistant", response.content,
                    MessageType.ASSISTANT, self.config.key, None, iteration,
                )

                text = "".join(b.text for b in response.content if b.type == "text")

                if response.stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    reason = (
                        f"the model declined this request "
                        f"(category: {getattr(details, 'category', 'unknown')})"
                    )
                    await self._set_status(task, TaskStatus.FAILED, reason)
                    return self._result(task, TaskStatus.FAILED, text, reason)

                if response.stop_reason == "max_tokens":
                    reason = (
                        f"the response hit the {self.settings.max_response_tokens} token "
                        "response cap and was truncated mid-output"
                    )
                    await self._set_status(task, TaskStatus.FAILED, reason)
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
                    await self._set_status(task, TaskStatus.COMPLETED)
                    log.info("[%s] completed in %d iterations", self.config.key, iteration)
                    return self._result(task, TaskStatus.COMPLETED, final_text, None)

                # stop_reason == "tool_use"
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    reason = f"unexpected stop_reason {response.stop_reason!r} with no tool calls"
                    await self._set_status(task, TaskStatus.FAILED, reason)
                    return self._result(task, TaskStatus.FAILED, text, reason)

                # Echo the assistant turn back verbatim — including thinking
                # blocks, which must be replayed unchanged on the same model.
                messages.append({"role": "assistant", "content": response.content})

                await self._persist_message(
                    task.id, "assistant", [_serialize(b) for b in tool_uses],
                    MessageType.TOOL_USE, self.config.key, None, iteration,
                )

                # Parallel tool calls run concurrently and every result goes
                # back in ONE user message; splitting them teaches the model to
                # stop calling tools in parallel.
                results = await asyncio.gather(
                    *(self._execute_tool(task.id, b) for b in tool_uses)
                )

                await self._persist_message(
                    task.id, "user", results, MessageType.TOOL_RESULT,
                    None, self.config.key, iteration,
                )
                messages.append({"role": "user", "content": results})

        except BudgetExceeded as exc:
            log.warning("[%s] %s", self.config.key, exc)
            await self._set_status(task, TaskStatus.BUDGET_EXCEEDED, str(exc))
            return self._result(task, TaskStatus.BUDGET_EXCEEDED, final_text, str(exc))
        except AgentRuntimeError as exc:
            log.error("[%s] %s", self.config.key, exc)
            await self._set_status(task, TaskStatus.FAILED, str(exc))
            return self._result(task, TaskStatus.FAILED, final_text, str(exc))

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
