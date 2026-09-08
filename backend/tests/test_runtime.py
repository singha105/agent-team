"""Agent runtime: loop termination, budget trips, and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select

from app.agents.config_loader import AgentConfig, load_agent_config
from app.agents.runtime import AgentRuntime, sync_agent_row
from app.models import HUMAN, Message, MessageType, Task, TaskStatus, ToolCall, Usage
from tests.fakes import FakeClient, FakeResponse, always_calls_a_tool, text_block, tool_use_block

AGENT_YAML = {
    "key": "backend",
    "display_name": "Ada",
    "role": "API and business logic",
    "model": "claude-opus-5",
    "avatar_id": "avatar-01",
    "tools": ["read_file", "write_file", "list_files", "run_command"],
    "system_prompt": "You are a backend engineer.",
}


@pytest.fixture
def agent(agent_config_dir: Path) -> AgentConfig:
    (agent_config_dir / "backend.yaml").write_text(yaml.safe_dump(AGENT_YAML), encoding="utf-8")
    return load_agent_config("backend", agent_config_dir)


async def make_task(session, agent: AgentConfig, description: str = "Do the thing") -> Task:
    row = await sync_agent_row(session, agent)
    task = Task(
        title=description[:80],
        description=description,
        assigned_agent_id=row.id,
        created_by=HUMAN,
    )
    session.add(task)
    await session.flush()
    return task


async def count(session, model, task_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(model).where(model.task_id == task_id)
        )
    ).scalar_one()


# -- termination -----------------------------------------------------------


async def test_loop_terminates_on_end_turn(session, agent, settings) -> None:
    client = FakeClient([FakeResponse([text_block("All done.")], "end_turn")])
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.NEEDS_REVIEW
    assert result.final_text == "All done."
    assert client.call_count == 1


async def test_loop_executes_a_tool_then_finishes(session, agent, settings, workspace) -> None:
    (workspace / "existing.py").write_text("x = 1")
    client = FakeClient(
        [
            FakeResponse([tool_use_block("list_files", {"path": "."})], "tool_use"),
            FakeResponse([text_block("Found existing.py.")], "end_turn"),
        ]
    )
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.NEEDS_REVIEW
    assert client.call_count == 2
    assert await count(session, ToolCall, task.id) == 1


async def test_loop_terminates_on_max_iterations(session, agent, settings) -> None:
    """A model that never stops calling tools must be halted by the budget."""
    settings.max_iterations = 5
    client = always_calls_a_tool()
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.BUDGET_EXCEEDED
    assert "iteration" in result.halt_reason
    assert client.call_count == 5, "must stop calling the API once the ceiling is reached"
    assert result.budget["iterations"] == 5


async def test_loop_terminates_on_token_budget(session, agent, settings) -> None:
    settings.max_iterations = 1000
    settings.max_tokens_per_task = 500
    client = always_calls_a_tool()
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.BUDGET_EXCEEDED
    assert "token" in result.halt_reason
    # 150 tokens per scripted call; the 4th check sees 450, the 5th sees 600.
    assert client.call_count == 4


async def test_per_agent_budget_override_wins(session, agent_config_dir, settings) -> None:
    (agent_config_dir / "backend.yaml").write_text(
        yaml.safe_dump(AGENT_YAML | {"max_iterations": 2}), encoding="utf-8"
    )
    agent = load_agent_config("backend", agent_config_dir)
    settings.max_iterations = 99
    client = always_calls_a_tool()
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.BUDGET_EXCEEDED
    assert client.call_count == 2


async def test_budget_status_is_persisted_with_a_reason(session, agent, settings) -> None:
    settings.max_iterations = 2
    task = await make_task(session, agent)

    await AgentRuntime(agent, session, settings, always_calls_a_tool()).run(task)

    refreshed = (await session.execute(select(Task).where(Task.id == task.id))).scalar_one()
    assert refreshed.status == TaskStatus.BUDGET_EXCEEDED
    assert "iteration" in refreshed.halt_reason


# -- non-tool stop reasons -------------------------------------------------


async def test_refusal_fails_the_task(session, agent, settings) -> None:
    class Details:
        category = "cyber"
        explanation = "declined"

    client = FakeClient([FakeResponse([text_block("")], "refusal", stop_details=Details())])
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.FAILED
    assert "declined" in result.halt_reason
    assert "cyber" in result.halt_reason


async def test_max_tokens_stop_fails_the_task(session, agent, settings) -> None:
    client = FakeClient([FakeResponse([text_block("half a th")], "max_tokens")])
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.FAILED
    assert "truncated" in result.halt_reason


async def test_pause_turn_is_resumed(session, agent, settings) -> None:
    client = FakeClient(
        [
            FakeResponse([text_block("thinking")], "pause_turn"),
            FakeResponse([text_block("done")], "end_turn"),
        ]
    )
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.NEEDS_REVIEW
    assert client.call_count == 2


async def test_endless_pause_turn_is_capped(session, agent, settings) -> None:
    client = FakeClient([FakeResponse([text_block("x")], "pause_turn")], repeat_last=True)
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.FAILED
    assert "paused" in result.halt_reason


# -- persistence -----------------------------------------------------------


async def test_every_step_is_persisted(session, agent, settings, workspace) -> None:
    client = FakeClient(
        [
            FakeResponse(
                [tool_use_block("write_file", {"path": "a.py", "content": "x=1"})], "tool_use"
            ),
            FakeResponse([text_block("Wrote a.py.")], "end_turn"),
        ]
    )
    task = await make_task(session, agent)

    await AgentRuntime(agent, session, settings, client).run(task)

    # user prompt, assistant turn, tool_use, tool_result, final assistant turn
    assert await count(session, Message, task.id) == 5
    assert await count(session, ToolCall, task.id) == 1
    assert await count(session, Usage, task.id) == 2

    types = (
        (
            await session.execute(
                select(Message.message_type).where(Message.task_id == task.id).order_by(Message.id)
            )
        )
        .scalars()
        .all()
    )
    assert types == [
        MessageType.USER,
        MessageType.ASSISTANT,
        MessageType.TOOL_USE,
        MessageType.TOOL_RESULT,
        MessageType.ASSISTANT,
    ]
    assert (workspace / "a.py").read_text() == "x=1"


async def test_tool_call_row_records_arguments_and_timing(session, agent, settings) -> None:
    client = FakeClient(
        [
            FakeResponse([tool_use_block("list_files", {"path": "."}, "toolu_abc")], "tool_use"),
            FakeResponse([text_block("done")], "end_turn"),
        ]
    )
    task = await make_task(session, agent)

    await AgentRuntime(agent, session, settings, client).run(task)

    call = (await session.execute(select(ToolCall).where(ToolCall.task_id == task.id))).scalar_one()
    assert call.tool_name == "list_files"
    assert call.tool_use_id == "toolu_abc"
    assert call.arguments == {"path": "."}
    assert call.duration_ms is not None
    assert call.error is None


async def test_usage_rows_carry_the_model_and_cost(session, agent, settings) -> None:
    client = FakeClient([FakeResponse([text_block("done")], "end_turn", 1_000_000, 0)])
    task = await make_task(session, agent)

    await AgentRuntime(agent, session, settings, client).run(task)

    usage = (await session.execute(select(Usage).where(Usage.task_id == task.id))).scalar_one()
    assert usage.model == "claude-opus-5"
    assert usage.input_tokens == 1_000_000
    assert usage.estimated_cost_usd == pytest.approx(5.00)


async def test_sandbox_denial_is_recorded_and_fed_back(session, agent, settings) -> None:
    """An escape attempt must reach the model as an error, not crash the run."""
    client = FakeClient(
        [
            FakeResponse(
                [tool_use_block("read_file", {"path": "../../../etc/passwd"})], "tool_use"
            ),
            FakeResponse([text_block("Understood, staying in the workspace.")], "end_turn"),
        ]
    )
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.NEEDS_REVIEW
    call = (await session.execute(select(ToolCall).where(ToolCall.task_id == task.id))).scalar_one()
    assert call.error is not None and "escapes the workspace" in call.error

    tool_result = (
        await session.execute(
            select(Message).where(
                Message.task_id == task.id, Message.message_type == MessageType.TOOL_RESULT
            )
        )
    ).scalar_one()
    assert tool_result.content[0]["is_error"] is True


# -- request shape ---------------------------------------------------------


async def test_request_carries_model_prompt_and_granted_tools(session, agent, settings) -> None:
    client = FakeClient([FakeResponse([text_block("done")], "end_turn")])
    task = await make_task(session, agent, "Build an endpoint")

    await AgentRuntime(agent, session, settings, client).run(task)

    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["system"] == "You are a backend engineer."
    # The first message now carries the shared PROJECT.md context ahead of the
    # task: agents must build against published decisions, and leaving them to
    # fetch it is an instruction they skip under pressure.
    first = call["messages"][0]
    assert first["role"] == "user"
    assert "Build an endpoint" in first["content"]
    assert "PROJECT.md" in first["content"]
    assert {t["name"] for t in call["tools"]} == set(AGENT_YAML["tools"])
    assert "thinking" not in call, "thinking must be left to the model default"
    assert call["output_config"] == {"effort": settings.effort}


async def test_agent_only_receives_the_tools_it_is_granted(session, agent_config_dir, settings):
    (agent_config_dir / "backend.yaml").write_text(
        yaml.safe_dump(AGENT_YAML | {"tools": ["read_file"]}), encoding="utf-8"
    )
    agent = load_agent_config("backend", agent_config_dir)
    client = FakeClient([FakeResponse([text_block("done")], "end_turn")])
    task = await make_task(session, agent)

    await AgentRuntime(agent, session, settings, client).run(task)

    assert [t["name"] for t in client.calls[0]["tools"]] == ["read_file"]


async def test_ungranted_tool_call_is_refused_without_crashing(session, agent_config_dir, settings):
    """If the model asks for a tool the agent lacks, say so and keep going."""
    (agent_config_dir / "backend.yaml").write_text(
        yaml.safe_dump(AGENT_YAML | {"tools": ["read_file"]}), encoding="utf-8"
    )
    agent = load_agent_config("backend", agent_config_dir)
    client = FakeClient(
        [
            FakeResponse([tool_use_block("run_command", {"command": "ls"})], "tool_use"),
            FakeResponse([text_block("understood")], "end_turn"),
        ]
    )
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.NEEDS_REVIEW
    call = (await session.execute(select(ToolCall).where(ToolCall.task_id == task.id))).scalar_one()
    assert "not available" in call.error


async def test_effort_off_omits_output_config(session, agent_config_dir, settings) -> None:
    (agent_config_dir / "backend.yaml").write_text(
        yaml.safe_dump(AGENT_YAML | {"effort": "off"}), encoding="utf-8"
    )
    agent = load_agent_config("backend", agent_config_dir)
    client = FakeClient([FakeResponse([text_block("done")], "end_turn")])
    task = await make_task(session, agent)

    await AgentRuntime(agent, session, settings, client).run(task)

    assert "output_config" not in client.calls[0]


async def test_parallel_tool_calls_return_in_one_user_message(session, agent, settings) -> None:
    """Splitting results across messages teaches the model to stop parallelising."""
    client = FakeClient(
        [
            FakeResponse(
                [
                    tool_use_block("list_files", {"path": "."}, "toolu_1"),
                    tool_use_block("write_file", {"path": "b.py", "content": "y=2"}, "toolu_2"),
                ],
                "tool_use",
            ),
            FakeResponse([text_block("both done")], "end_turn"),
        ]
    )
    task = await make_task(session, agent)

    await AgentRuntime(agent, session, settings, client).run(task)

    assert await count(session, ToolCall, task.id) == 2
    second_request_messages = client.calls[1]["messages"]
    tool_result_messages = [
        m for m in second_request_messages if m["role"] == "user" and isinstance(m["content"], list)
    ]
    assert len(tool_result_messages) == 1
    assert len(tool_result_messages[0]["content"]) == 2


async def test_conversation_history_grows_across_iterations(session, agent, settings) -> None:
    client = FakeClient(
        [
            FakeResponse([tool_use_block("list_files", {"path": "."}, "t1")], "tool_use"),
            FakeResponse([tool_use_block("list_files", {"path": "."}, "t2")], "tool_use"),
            FakeResponse([text_block("done")], "end_turn"),
        ]
    )
    task = await make_task(session, agent)

    await AgentRuntime(agent, session, settings, client).run(task)

    lengths = [len(c["messages"]) for c in client.calls]
    assert lengths == [1, 3, 5], "each turn appends the assistant turn and its tool results"


async def test_unpriced_model_fails_before_any_api_call(session, agent_config_dir, settings):
    """Changing a model is a one-line config edit, so a model with no pricing
    entry is a realistic mistake. It must fail cleanly and before spending."""
    (agent_config_dir / "backend.yaml").write_text(
        yaml.safe_dump(AGENT_YAML | {"model": "claude-not-in-pricing-table"}), encoding="utf-8"
    )
    agent = load_agent_config("backend", agent_config_dir)
    client = FakeClient([FakeResponse([text_block("done")], "end_turn")])
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.FAILED
    assert "pricing.yaml" in result.halt_reason
    assert client.call_count == 0, "must not spend before discovering it cannot cost the run"


async def test_unexpected_exception_never_strands_a_task_in_running(
    session, agent, settings, monkeypatch
) -> None:
    """A task left in RUNNING has no status and no reason — the trace just
    stops. Any unforeseen error must still close it out."""
    client = FakeClient([FakeResponse([text_block("done")], "end_turn")])
    runtime = AgentRuntime(agent, session, settings, client)

    def boom(*_a, **_kw):
        raise ValueError("something nobody predicted")

    monkeypatch.setattr(runtime, "_persist_usage", boom)
    task = await make_task(session, agent)

    result = await runtime.run(task)

    assert result.status == TaskStatus.FAILED
    assert "ValueError" in result.halt_reason
    refreshed = (await session.execute(select(Task).where(Task.id == task.id))).scalar_one()
    assert refreshed.status == TaskStatus.FAILED
    assert refreshed.status != TaskStatus.IN_PROGRESS
