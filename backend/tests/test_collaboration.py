"""Inter-agent collaboration: handoff, cycles, hop limits and cost rollup.

Driven through the real runtime, the real message bus and the real database.
Each agent gets its own scripted model, so a two-agent handoff runs end to end
without an API key.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents.project_context import read_context
from app.agents.rollup import root_task_id_for, tree_usage
from app.agents.runtime import AgentRuntime, sync_agent_row
from app.core import db as db_module
from app.models import HUMAN, Agent, Message, MessageType, Task, TaskStatus
from tests.fakes import FakeClient, FakeResponse, text_block, tool_use_block

REAL_AGENT_CONFIGS = Path(__file__).resolve().parents[2] / "config" / "agents"


@pytest.fixture
def collab_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "data").mkdir()
    agents = tmp_path / "agents"
    shutil.copytree(REAL_AGENT_CONFIGS, agents)

    monkeypatch.setenv("AGENTTEAM_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("AGENTTEAM_DB_PATH", str(tmp_path / "data" / "collab.db"))
    monkeypatch.setenv("AGENTTEAM_AGENT_CONFIG_DIR", str(agents))
    monkeypatch.setenv("AGENTTEAM_SANDBOX_MODE", "subprocess")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from app.agents import config_loader
    from app.core import config as cfg

    cfg.get_settings.cache_clear()
    config_loader._cached.cache_clear()
    db_module._engine = None
    db_module._session_factory = None
    yield cfg.get_settings()
    config_loader._cached.cache_clear()
    cfg.get_settings.cache_clear()
    db_module._engine = None
    db_module._session_factory = None


@pytest_asyncio.fixture
async def session(collab_settings):
    import app.models  # noqa: F401

    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_module.Base.metadata.create_all)
    factory = db_module.get_session_factory()
    async with factory() as s:
        yield s
    await db_module.dispose_engine()


class Scripts:
    """Per-agent scripted models, recording what each one was sent."""

    def __init__(self, scripts: dict[str, list[FakeResponse]]) -> None:
        self.scripts = scripts
        self.clients: dict[str, FakeClient] = {}

    def __call__(self, agent_key: str) -> FakeClient:
        client = FakeClient(
            self.scripts.get(agent_key, [FakeResponse([text_block("nothing to do")], "end_turn")])
        )
        self.clients[agent_key] = client
        return client


async def make_task(session, agent_key: str, description: str) -> Task:
    from app.agents.config_loader import get_agent_config

    row = await sync_agent_row(session, get_agent_config(agent_key))
    task = Task(
        title=description[:80],
        description=description,
        assigned_agent_id=row.id,
        created_by=HUMAN,
        status=TaskStatus.QUEUED,
    )
    session.add(task)
    await session.flush()
    await session.commit()
    return task


async def run(session, settings, agent_key: str, task: Task, scripts: Scripts):
    from app.agents.config_loader import get_agent_config

    runtime = AgentRuntime(
        get_agent_config(agent_key),
        session,
        settings,
        client=scripts(agent_key),
        client_for=scripts,
    )
    return await runtime.run(task)


# -- the headline case ----------------------------------------------------


SCHEMA_ANSWER = (
    "books(id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, "
    "year INTEGER NOT NULL). Index on (title, author) for search."
)


def handoff_scripts() -> Scripts:
    """Backend asks Database for a schema, then builds against the answer."""
    return Scripts(
        {
            "backend": [
                FakeResponse(
                    [
                        tool_use_block(
                            "ask_agent",
                            {
                                "to_agent": "database",
                                "question": "I need a schema for a book library with search. "
                                "What tables and columns should I build against?",
                            },
                            "t_ask",
                        )
                    ],
                    "tool_use",
                    3000,
                    200,
                ),
                FakeResponse(
                    [
                        tool_use_block(
                            "write_file",
                            {
                                "path": "api/books.py",
                                "content": "# built on the published schema\n",
                            },
                            "t_write",
                        )
                    ],
                    "tool_use",
                    3500,
                    400,
                ),
                FakeResponse(
                    [
                        tool_use_block(
                            "append_project_context",
                            {
                                "heading": "API contract: books",
                                "body": "GET /books?q= -> list[Book]; GET /books/{id} -> Book",
                            },
                            "t_pub",
                        )
                    ],
                    "tool_use",
                    3800,
                    150,
                ),
                FakeResponse(
                    [text_block("Asked the data agent for the schema and built against it.")],
                    "end_turn",
                    4000,
                    120,
                ),
            ],
            "database": [
                FakeResponse(
                    [
                        tool_use_block(
                            "append_project_context",
                            {"heading": "Books table schema", "body": SCHEMA_ANSWER},
                            "t_pub_schema",
                        )
                    ],
                    "tool_use",
                    2000,
                    180,
                ),
                FakeResponse([text_block(SCHEMA_ANSWER)], "end_turn", 2200, 90),
            ],
        }
    )


async def test_two_agent_handoff_completes(session, collab_settings) -> None:
    scripts = handoff_scripts()
    task = await make_task(session, "backend", "Build a REST API for a book library with search")

    result = await run(session, collab_settings, "backend", task, scripts)

    assert result.status == TaskStatus.NEEDS_REVIEW
    assert "database" in scripts.clients, "the backend agent never delegated"


async def test_the_handoff_creates_a_linked_child_task(session, collab_settings) -> None:
    scripts = handoff_scripts()
    task = await make_task(session, "backend", "Build a book library API")
    await run(session, collab_settings, "backend", task, scripts)

    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id))).scalars().all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.created_by == "backend"
    assert child.status == TaskStatus.NEEDS_REVIEW

    agent = (
        await session.execute(select(Agent).where(Agent.id == child.assigned_agent_id))
    ).scalar_one()
    assert agent.key == "database"

    assert await root_task_id_for(session, child.id) == task.id


async def test_the_answer_comes_back_as_the_tool_result(session, collab_settings) -> None:
    """The whole point of ask_agent: the caller resumes with the answer."""
    scripts = handoff_scripts()
    task = await make_task(session, "backend", "Build a book library API")
    await run(session, collab_settings, "backend", task, scripts)

    backend_calls = scripts.clients["backend"].calls
    # The second request carries the tool_result for the ask.
    tool_results = [
        block
        for call in backend_calls
        for message in call["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert tool_results, "the caller never received a tool result"
    answer = str(tool_results[0]["content"])
    assert "books(" in answer
    assert not tool_results[0]["is_error"]


async def test_the_conversation_is_persisted_in_both_directions(session, collab_settings) -> None:
    scripts = handoff_scripts()
    task = await make_task(session, "backend", "Build a book library API")
    await run(session, collab_settings, "backend", task, scripts)

    exchanges = (
        (
            await session.execute(
                select(Message)
                .where(Message.message_type == MessageType.AGENT_TO_AGENT)
                .order_by(Message.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(exchanges) == 2, "a question and an answer should both be recorded"
    question, answer = exchanges
    assert (question.from_agent, question.to_agent) == ("backend", "database")
    assert (answer.from_agent, answer.to_agent) == ("database", "backend")
    assert "books(" in str(answer.content)


async def test_the_child_reads_the_shared_context(session, collab_settings) -> None:
    """The data agent publishes the schema; the backend agent's later turns see
    it, and so would any future task."""
    scripts = handoff_scripts()
    task = await make_task(session, "backend", "Build a book library API")
    await run(session, collab_settings, "backend", task, scripts)

    published = read_context(collab_settings)
    assert "Books table schema" in published
    assert "API contract: books" in published
    assert "added by **database**" in published
    assert "added by **backend**" in published


# -- cost rollup -----------------------------------------------------------


async def test_child_usage_rolls_up_to_the_root(session, collab_settings) -> None:
    scripts = handoff_scripts()
    task = await make_task(session, "backend", "Build a book library API")
    result = await run(session, collab_settings, "backend", task, scripts)

    rollup = await tree_usage(session, task.id)

    assert rollup.delegated_task_count == 1
    assert len(rollup.task_ids) == 2

    # The tree must cost strictly more than the root task alone.
    assert rollup.estimated_cost_usd > result.budget["estimated_cost_usd"]
    assert rollup.total_tokens > result.budget["total_tokens"]

    agents = {a.agent_key: a for a in rollup.by_agent}
    assert set(agents) == {"backend", "database"}
    assert agents["backend"].model == "claude-opus-5"
    assert agents["database"].model == "claude-sonnet-5"
    # Both models are represented, so the rollup is not silently pricing the
    # child at the parent's rate.
    assert agents["backend"].estimated_cost_usd > 0
    assert agents["database"].estimated_cost_usd > 0


async def test_rollup_sums_to_the_parts(session, collab_settings) -> None:
    scripts = handoff_scripts()
    task = await make_task(session, "backend", "Build a book library API")
    await run(session, collab_settings, "backend", task, scripts)

    rollup = await tree_usage(session, task.id)
    per_agent_total = sum(a.estimated_cost_usd for a in rollup.by_agent)
    assert per_agent_total == pytest.approx(rollup.estimated_cost_usd, abs=1e-6)


# -- loop protection -------------------------------------------------------


def ping_pong_scripts() -> Scripts:
    """A deliberately hostile pair: each agent immediately asks the other."""
    ask_db = FakeResponse(
        [tool_use_block("ask_agent", {"to_agent": "database", "question": "your turn"}, "a1")],
        "tool_use",
    )
    ask_backend = FakeResponse(
        [tool_use_block("ask_agent", {"to_agent": "backend", "question": "no, your turn"}, "a2")],
        "tool_use",
    )
    return Scripts(
        {
            "backend": [ask_db, FakeResponse([text_block("gave up asking")], "end_turn")],
            "database": [
                ask_backend,
                FakeResponse([text_block("could not ask back; answering directly")], "end_turn"),
            ],
        }
    )


async def test_a_deliberate_loop_is_caught_and_terminated(session, collab_settings) -> None:
    """A <-> B must not run away. The return hop is refused and both agents
    finish, rather than the run hanging or recursing."""
    scripts = ping_pong_scripts()
    task = await make_task(session, "backend", "Start a ping-pong")

    result = await run(session, collab_settings, "backend", task, scripts)

    assert result.status == TaskStatus.NEEDS_REVIEW

    # Exactly one child: the refused return hop never created a second.
    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id))).scalars().all()
    )
    assert len(children) == 1

    from app.models import ToolCall

    refusals = (
        (
            await session.execute(
                select(ToolCall).where(
                    ToolCall.tool_name == "ask_agent", ToolCall.error.isnot(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert refusals, "the cycle was never refused"
    assert "cycle" in refusals[0].error.lower()


async def test_the_refusal_is_reported_to_the_agent_not_raised(session, collab_settings) -> None:
    """A refused delegation must let the agent finish, not kill the run."""
    scripts = ping_pong_scripts()
    task = await make_task(session, "backend", "Start a ping-pong")
    await run(session, collab_settings, "backend", task, scripts)

    database_calls = scripts.clients["database"].calls
    results = [
        block
        for call in database_calls
        for message in call["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert results
    assert results[0]["is_error"] is True
    assert "cycle" in str(results[0]["content"]).lower()


async def test_hop_limit_stops_a_delegation_chain(session, collab_settings, monkeypatch) -> None:
    monkeypatch.setattr(collab_settings, "max_agent_hops", 1)

    scripts = Scripts(
        {
            "backend": [
                FakeResponse(
                    [tool_use_block("ask_agent", {"to_agent": "database", "question": "q1"}, "a")],
                    "tool_use",
                ),
                FakeResponse(
                    [tool_use_block("ask_agent", {"to_agent": "devops", "question": "q2"}, "b")],
                    "tool_use",
                ),
                FakeResponse([text_block("stopped asking")], "end_turn"),
            ],
            "database": [FakeResponse([text_block("here is the schema")], "end_turn")],
        }
    )
    task = await make_task(session, "backend", "Ask everyone")

    result = await run(session, collab_settings, "backend", task, scripts)

    assert result.status == TaskStatus.NEEDS_REVIEW
    assert "devops" not in scripts.clients, "the hop limit did not stop the second ask"

    from app.models import ToolCall

    errors = [
        c.error
        for c in (
            (await session.execute(select(ToolCall).where(ToolCall.tool_name == "ask_agent")))
            .scalars()
            .all()
        )
        if c.error
    ]
    assert any("hop limit" in e for e in errors)


async def test_wall_clock_deadline_halts_the_tree(session, collab_settings, monkeypatch) -> None:
    monkeypatch.setattr(collab_settings, "task_deadline_seconds", 0.001)
    scripts = Scripts({"backend": [FakeResponse([text_block("done")], "end_turn")]})
    task = await make_task(session, "backend", "Anything")

    # asyncio.sleep, not time.sleep: blocking the event loop inside an async
    # test would also stall the runtime under test.
    await asyncio.sleep(0.01)
    result = await run(session, collab_settings, "backend", task, scripts)

    assert result.status == TaskStatus.BUDGET_EXCEEDED
    assert "wall-clock" in result.halt_reason


# -- guards ----------------------------------------------------------------


async def test_an_agent_cannot_message_itself(session, collab_settings) -> None:
    scripts = Scripts(
        {
            "backend": [
                FakeResponse(
                    [tool_use_block("ask_agent", {"to_agent": "backend", "question": "hi"}, "s")],
                    "tool_use",
                ),
                FakeResponse([text_block("understood")], "end_turn"),
            ]
        }
    )
    task = await make_task(session, "backend", "Talk to yourself")
    await run(session, collab_settings, "backend", task, scripts)

    from app.models import ToolCall

    call = (
        await session.execute(select(ToolCall).where(ToolCall.tool_name == "ask_agent"))
    ).scalar_one()
    assert call.error is not None
    assert "itself" in call.error


async def test_an_unknown_recipient_is_refused_with_the_roster(session, collab_settings) -> None:
    scripts = Scripts(
        {
            "backend": [
                FakeResponse(
                    [
                        tool_use_block(
                            "send_message", {"to_agent": "marketing", "content": "hello"}, "s"
                        )
                    ],
                    "tool_use",
                ),
                FakeResponse([text_block("understood")], "end_turn"),
            ]
        }
    )
    task = await make_task(session, "backend", "Message a stranger")
    await run(session, collab_settings, "backend", task, scripts)

    from app.models import ToolCall

    call = (
        await session.execute(select(ToolCall).where(ToolCall.tool_name == "send_message"))
    ).scalar_one()
    assert call.error is not None
    assert "marketing" in call.error
    assert "database" in call.error, "the refusal should name who is available"


async def test_send_message_is_recorded_but_blocks_nothing(session, collab_settings) -> None:
    scripts = Scripts(
        {
            "backend": [
                FakeResponse(
                    [
                        tool_use_block(
                            "send_message",
                            {"to_agent": "frontend", "content": "contract is published"},
                            "s",
                        )
                    ],
                    "tool_use",
                ),
                FakeResponse([text_block("told them")], "end_turn"),
            ]
        }
    )
    task = await make_task(session, "backend", "Notify the UI agent")
    await run(session, collab_settings, "backend", task, scripts)

    assert "frontend" not in scripts.clients, "a notification must not run the recipient"

    note = (
        await session.execute(
            select(Message).where(Message.message_type == MessageType.AGENT_TO_AGENT)
        )
    ).scalar_one()
    assert (note.from_agent, note.to_agent) == ("backend", "frontend")

    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id))).scalars().all()
    )
    assert children == [], "a notification must not create a child task"


# -- through the HTTP path -------------------------------------------------


async def test_delegation_works_end_to_end_through_the_api(api_settings, monkeypatch) -> None:
    """The exit criterion: one task to Backend, and the team collaborates with
    no further human input.

    Driven through the real router and worker rather than the runtime directly,
    so the client factory reaching a delegated run is exercised too.
    """
    from httpx import ASGITransport, AsyncClient

    import app.models  # noqa: F401
    from app.events.bus import EventBus
    from app.main import app as fastapi_app
    from app.workers import queue as queue_module
    from app.workers.queue import TaskWorkerPool

    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_module.Base.metadata.create_all)

    scripts = handoff_scripts()
    bus = EventBus()
    monkeypatch.setattr("app.events.bus._bus", bus)
    monkeypatch.setattr("app.api.tasks.get_event_bus", lambda: bus)
    pool = TaskWorkerPool(settings=api_settings, bus=bus, client_factory=scripts)
    monkeypatch.setattr(queue_module, "_pool", pool)
    monkeypatch.setattr("app.api.tasks.get_worker_pool", lambda: pool)
    await pool.start(["backend", "database", "devops", "frontend"])

    try:
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = (
                await client.post(
                    "/api/tasks",
                    json={
                        "agent_key": "backend",
                        "description": "Build a REST API for a book library with search",
                    },
                )
            ).json()
            assert await pool.wait_until_idle(timeout=30)

            detail = (await client.get(f"/api/tasks/{created['id']}")).json()

            assert detail["status"] == "needs_review"
            # The delegated run got its own agent's client, not the caller's.
            assert "database" in scripts.clients

            depths = {n["agent_key"]: n["depth"] for n in detail["delegation"]}
            assert depths == {"backend": 0, "database": 1}

            tree = detail["tree"]
            assert tree["delegated_task_count"] == 1
            assert tree["estimated_cost_usd"] > detail["total_cost_usd"]
            models = {a["agent_key"]: a["model"] for a in tree["by_agent"]}
            assert models == {"backend": "claude-opus-5", "database": "claude-sonnet-5"}

            exchanges = [m for m in detail["messages"] if m["message_type"] == "agent_to_agent"]
            assert len(exchanges) == 2
            assert (exchanges[0]["from_agent"], exchanges[0]["to_agent"]) == (
                "backend",
                "database",
            )
    finally:
        await pool.stop()
        await db_module.dispose_engine()
