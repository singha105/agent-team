"""Things that go wrong at demo time if nobody handled them.

Malformed tool arguments, concurrent writes to one file, and results too large
for a context window are all normal behaviour under a team of agents, not
exceptional. Each has to degrade into a message the agent can act on rather
than into a dead run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.agents.config_loader import AgentConfig, load_agent_config
from app.agents.tools import build_toolset
from app.agents.tools.filesystem import write_file
from app.agents.tools.validation import ArgumentError, validate_arguments
from app.agents.workspace_locks import WorkspaceLocks
from app.models import TaskStatus, ToolCall
from tests.fakes import FakeClient, FakeResponse, text_block, tool_use_block

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


# -- malformed tool arguments ---------------------------------------------


def schema_for(tool_name: str) -> dict:
    schemas, _ = build_toolset([tool_name])
    return schemas[0]["input_schema"]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "missing required"),
        ({"path": 5}, "must be string"),
        ({"path": "a.py", "nope": 1}, "unknown argument"),
        ({"path": "a.py", "offset": "3"}, "must be integer"),
        ({"path": "a.py", "offset": True}, "got boolean"),
        ({"path": "a.py", "offset": -1}, "at least 0"),
    ],
)
def test_invalid_arguments_are_described_precisely(arguments: dict, expected: str) -> None:
    """The message is read by the model, so it has to say what to change."""
    with pytest.raises(ArgumentError) as exc:
        validate_arguments("read_file", schema_for("read_file"), arguments)
    assert expected in str(exc.value)
    assert "try again" in str(exc.value)


def test_non_object_arguments_are_rejected() -> None:
    with pytest.raises(ArgumentError, match="expects an object"):
        validate_arguments("read_file", schema_for("read_file"), ["a.py"])


def test_valid_arguments_pass_through_unchanged() -> None:
    args = {"path": "a.py", "offset": 0, "limit": 20}
    assert validate_arguments("read_file", schema_for("read_file"), args) == args


def test_optional_arguments_may_be_omitted() -> None:
    assert validate_arguments("read_file", schema_for("read_file"), {"path": "a.py"})


async def test_a_malformed_call_costs_one_iteration_not_the_run(session, agent, settings) -> None:
    """The model emitting bad arguments is normal. The run must survive it and
    the agent must get back something it can correct from."""
    from app.agents.runtime import AgentRuntime
    from tests.test_runtime import make_task

    client = FakeClient(
        [
            FakeResponse([tool_use_block("read_file", {"wrong_arg": "x"}, "t1")], "tool_use"),
            FakeResponse([text_block("Corrected and finished.")], "end_turn"),
        ]
    )
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client).run(task)

    assert result.status == TaskStatus.NEEDS_REVIEW, "a bad argument killed the run"

    call = (
        await session.execute(select(ToolCall).where(ToolCall.tool_name == "read_file"))
    ).scalar_one()
    assert call.error is not None
    assert "missing required" in call.error
    assert call.result["invalid_arguments"] is True

    # The correction reached the model as a tool_result it can act on.
    second_request = client.calls[1]["messages"]
    results = [
        block
        for message in second_request
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert results and results[0]["is_error"] is True


# -- concurrent workspace writes -------------------------------------------


async def test_writes_to_one_path_are_serialised(settings, workspace: Path) -> None:
    """Two agents writing the same file is the one way a run destroys work
    rather than merely failing. Each write must land whole."""
    from app.agents import workspace_locks as module

    module._locks = WorkspaceLocks()

    bodies = [f"content from agent {i}\n" * 200 for i in range(6)]
    outcomes = await asyncio.gather(*(write_file("shared.py", body) for body in bodies))

    assert all(not o.is_error for o in outcomes)

    # Exactly one writer's content survives, intact — not a blend of six.
    final = (workspace / "shared.py").read_text()
    assert final in bodies, "the file interleaved instead of being written atomically"


async def test_replacing_another_agents_file_is_reported(settings, workspace: Path) -> None:
    """The realistic conflict is not two writes racing — writes are fast. It is
    one agent replacing another's file later, with no contention at all and the
    first agent's work silently gone."""
    from app.agents import workspace_locks as module
    from app.agents.current_agent import set_current_agent

    module._locks = WorkspaceLocks()

    set_current_agent("database")
    first = await write_file("schema.sql", "CREATE TABLE books (id INTEGER);\n")
    assert not first.payload["conflict"]

    set_current_agent("backend")
    second = await write_file("schema.sql", "CREATE TABLE users (id INTEGER);\n")

    assert second.payload["conflict"] is True
    assert second.payload["replaced_writer"] == "database"
    assert "database wrote this file" in second.content
    assert "replaced their content" in second.content
    assert "merge" in second.content

    assert module._locks.conflicts, "the conflict was not recorded for the trace"


async def test_an_agent_revising_its_own_file_is_not_a_conflict(settings, workspace: Path) -> None:
    """Ordinary work — an agent iterating on a file it owns — must not be
    reported as someone clobbering someone else."""
    from app.agents import workspace_locks as module
    from app.agents.current_agent import set_current_agent

    module._locks = WorkspaceLocks()
    set_current_agent("backend")

    await write_file("api.py", "v1\n")
    second = await write_file("api.py", "v2\n")

    assert second.payload["conflict"] is False
    assert module._locks.conflicts == []


async def test_writing_identical_content_is_not_a_conflict(settings, workspace: Path) -> None:
    """Nothing was lost, so there is nothing to warn about."""
    from app.agents import workspace_locks as module
    from app.agents.current_agent import set_current_agent

    module._locks = WorkspaceLocks()

    set_current_agent("database")
    await write_file("shared.py", "same bytes\n")
    set_current_agent("backend")
    second = await write_file("shared.py", "same bytes\n")

    assert second.payload["conflict"] is False


async def test_writes_to_different_paths_do_not_conflict(settings, workspace: Path) -> None:
    from app.agents import workspace_locks as module
    from app.agents.current_agent import set_current_agent

    module._locks = WorkspaceLocks()
    set_current_agent("backend")

    outcomes = await asyncio.gather(*(write_file(f"file{i}.py", f"x{i}") for i in range(8)))

    assert all(not o.payload["conflict"] for o in outcomes)
    assert module._locks.conflicts == []


async def test_the_lock_registry_itself_is_race_free() -> None:
    """Two coroutines reaching an unlocked path at once must not each create a
    lock — neither would exclude the other, which is the bug the class exists
    to prevent."""
    locks = WorkspaceLocks()
    path = Path("/tmp/agentteam-lock-probe")

    async def take() -> None:
        lock = await locks.acquire(path)
        await asyncio.sleep(0)
        lock.release()

    await asyncio.gather(*(take() for _ in range(12)))
    assert len(locks._locks) == 1


async def test_concurrent_agents_do_not_see_each_others_identity() -> None:
    """The ambient agent key is a ContextVar, so each asyncio task gets its own
    copy. If it leaked between tasks, every conflict would name the wrong agent."""
    from app.agents.current_agent import current_agent, set_current_agent

    seen: dict[str, str] = {}

    async def act(key: str) -> None:
        set_current_agent(key)
        await asyncio.sleep(0.01)
        seen[key] = current_agent()

    await asyncio.gather(*(act(k) for k in ["backend", "database", "devops", "frontend"]))
    assert seen == {k: k for k in ["backend", "database", "devops", "frontend"]}


# -- rate limits and overloads ---------------------------------------------


def test_retry_delay_honours_retry_after() -> None:
    """The server knows how long it wants to be left alone better than any
    formula does."""
    from app.agents.runtime import _retry_delay

    class Response:
        headers = {"retry-after": "12"}

    class Limited(Exception):
        response = Response()

    assert _retry_delay(Limited(), attempt=1) == 12.0


def test_retry_delay_backs_off_and_jitters() -> None:
    """Without jitter every concurrent agent retries in the same instant and
    reproduces the overload that caused the backoff."""
    from app.agents.runtime import RETRY_MAX_SECONDS, _retry_delay

    class Bare(Exception):
        response = None

    samples = {_retry_delay(Bare(), attempt=5) for _ in range(40)}
    assert len(samples) > 5, "delays are not jittered"
    assert all(0 <= s <= RETRY_MAX_SECONDS for s in samples)

    late = max(_retry_delay(Bare(), attempt=8) for _ in range(80))
    early = max(_retry_delay(Bare(), attempt=1) for _ in range(80))
    assert late > early, "the delay does not grow with the attempt"


def test_retry_delay_survives_a_malformed_header() -> None:
    from app.agents.runtime import _retry_delay

    class Response:
        headers = {"retry-after": "soon-ish"}

    class Limited(Exception):
        response = Response()

    assert _retry_delay(Limited(), attempt=1) >= 0


async def test_a_rate_limit_is_retried_and_announced(session, agent, settings) -> None:
    """A run patiently waiting out a rate limit must be distinguishable from a
    run that has hung — which means something has to know it is waiting."""
    import anthropic
    import httpx

    from app.agents.runtime import AgentRuntime
    from app.events.bus import EventBus
    from tests.test_runtime import make_task

    limited = anthropic.RateLimitError(
        "slow down",
        response=httpx.Response(
            429, headers={"retry-after": "0"}, request=httpx.Request("POST", "http://x")
        ),
        body=None,
    )

    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0
            self.messages = self

        async def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise limited
            return FakeResponse([text_block("recovered")], "end_turn")

    bus = EventBus()
    task = await make_task(session, agent)
    runtime = AgentRuntime(agent, session, settings, client=FlakyClient(), bus=bus)

    result = await runtime.run(task)

    assert result.status == TaskStatus.NEEDS_REVIEW, "the retry did not recover the run"

    waits = [e for e in bus.history() if e.type == "agent.waiting"]
    assert waits, "the wait was never announced, so the UI could not show it"
    assert waits[0].reason == "rate limited"
    assert waits[0].agent_key == agent.key
    assert waits[0].max_attempts == settings.api_max_retries


async def test_retries_are_capped(session, agent, settings, monkeypatch) -> None:
    """A service that is genuinely down must not be hammered forever."""
    import anthropic
    import httpx

    from app.agents.runtime import AgentRuntime
    from tests.test_runtime import make_task

    monkeypatch.setattr(settings, "api_max_retries", 2)

    class AlwaysLimited:
        def __init__(self) -> None:
            self.calls = 0
            self.messages = self

        async def create(self, **_kwargs):
            self.calls += 1
            raise anthropic.RateLimitError(
                "no",
                response=httpx.Response(
                    429,
                    headers={"retry-after": "0"},
                    request=httpx.Request("POST", "http://x"),
                ),
                body=None,
            )

    client = AlwaysLimited()
    task = await make_task(session, agent)

    result = await AgentRuntime(agent, session, settings, client=client).run(task)

    assert result.status == TaskStatus.FAILED
    assert "backed-off retries" in result.halt_reason
    # The original call plus exactly the permitted retries.
    assert client.calls == 3
