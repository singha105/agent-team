"""Notifications wake an idle recipient — and cannot start a storm.

send_message stays fire-and-forget for the sender: it does not wait. But the
message no longer sits unread until the recipient happens to get another task —
a follow-up task is queued so an idle agent actually acts on it.

That makes an unbounded notification an unbounded amount of *work*, so the
containment tests here matter as much as the delivery ones.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.models  # noqa: F401
from app.core import db as db_module
from app.events.bus import EventBus
from app.main import app as fastapi_app
from app.models import Message, MessageType, Task, ToolCall
from app.workers import queue as queue_module
from app.workers.queue import TaskWorkerPool
from tests.fakes import FakeClient, FakeResponse, text_block, tool_use_block

REAL_AGENT_CONFIGS = Path(__file__).resolve().parents[2] / "config" / "teams" / "software"
AGENT_KEYS = ["backend", "database", "devops", "frontend"]


@pytest.fixture
def wake_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "data").mkdir()
    agents = tmp_path / "agents"
    shutil.copytree(REAL_AGENT_CONFIGS, agents)

    monkeypatch.setenv("AGENTTEAM_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("AGENTTEAM_DB_PATH", str(tmp_path / "data" / "wake.db"))
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


class Scripts:
    def __init__(self, scripts: dict[str, list[FakeResponse]], default_repeat: bool = False):
        self.scripts = scripts
        self.default_repeat = default_repeat
        self.calls: list[str] = []

    def __call__(self, agent_key: str) -> FakeClient:
        self.calls.append(agent_key)
        script = self.scripts.get(
            agent_key, [FakeResponse([text_block("Noted, nothing for me to do.")], "end_turn")]
        )
        return FakeClient(script, repeat_last=self.default_repeat)


@pytest_asyncio.fixture
async def api(wake_settings, monkeypatch):
    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_module.Base.metadata.create_all)

    bus = EventBus()
    monkeypatch.setattr("app.events.bus._bus", bus)
    monkeypatch.setattr("app.api.tasks.get_event_bus", lambda: bus)

    def make(scripts: Scripts) -> TaskWorkerPool:
        pool = TaskWorkerPool(settings=wake_settings, bus=bus, client_factory=scripts)
        monkeypatch.setattr(queue_module, "_pool", pool)
        monkeypatch.setattr("app.api.tasks.get_worker_pool", lambda: pool)
        return pool

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.make_pool = make  # type: ignore[attr-defined]
        client.bus = bus  # type: ignore[attr-defined]
        client.settings = wake_settings  # type: ignore[attr-defined]
        yield client

    await db_module.dispose_engine()


def notify_once(to_agent: str, content: str) -> list[FakeResponse]:
    return [
        FakeResponse(
            [tool_use_block("send_message", {"to_agent": to_agent, "content": content}, "n1")],
            "tool_use",
        ),
        FakeResponse([text_block("Told them.")], "end_turn"),
    ]


async def session_for(api):
    return db_module.get_session_factory()()


# -- delivery --------------------------------------------------------------


async def test_a_notification_wakes_an_idle_recipient(api) -> None:
    scripts = Scripts({"backend": notify_once("frontend", "The API contract is published.")})
    pool = api.make_pool(scripts)
    await pool.start(AGENT_KEYS)
    try:
        created = (
            await api.post(
                "/api/tasks", json={"agent_key": "backend", "description": "Publish the contract"}
            )
        ).json()
        assert await pool.wait_until_idle(timeout=30)
    finally:
        await pool.stop()

    # The recipient ran, without the manager assigning it anything.
    assert "frontend" in scripts.calls

    detail = (await api.get(f"/api/tasks/{created['id']}")).json()
    agents = {n["agent_key"] for n in detail["delegation"]}
    assert agents == {"backend", "frontend"}


async def test_the_woken_task_is_linked_into_the_tree(api) -> None:
    scripts = Scripts({"backend": notify_once("frontend", "Contract published.")})
    pool = api.make_pool(scripts)
    await pool.start(AGENT_KEYS)
    try:
        created = (
            await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
        ).json()
        assert await pool.wait_until_idle(timeout=30)
    finally:
        await pool.stop()

    detail = (await api.get(f"/api/tasks/{created['id']}")).json()
    nodes = {n["agent_key"]: n for n in detail["delegation"]}
    assert nodes["frontend"]["parent_task_id"] == created["id"]
    assert nodes["frontend"]["depth"] == 1
    assert nodes["frontend"]["created_by"] == "backend"

    # Cost rolls up, because the woken work is part of this task's cost.
    assert detail["tree"]["delegated_task_count"] == 1
    assert detail["tree"]["estimated_cost_usd"] > detail["total_cost_usd"]


async def test_the_woken_agent_receives_the_message_text(api) -> None:
    scripts = Scripts({"backend": notify_once("frontend", "GET /books returns list[Book].")})
    pool = api.make_pool(scripts)
    await pool.start(AGENT_KEYS)
    try:
        created = (
            await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
        ).json()
        assert await pool.wait_until_idle(timeout=30)
    finally:
        await pool.stop()

    detail = (await api.get(f"/api/tasks/{created['id']}")).json()
    woken_id = next(n["task_id"] for n in detail["delegation"] if n["agent_key"] == "frontend")
    woken = (await api.get(f"/api/tasks/{woken_id}")).json()

    assert "GET /books returns list[Book]." in woken["description"]
    # Framed as a notification, so an FYI does not become a second project.
    assert "Do not invent work" in woken["description"]


async def test_the_sender_does_not_wait(api) -> None:
    """Fire-and-forget must stay fire-and-forget: the sender's own run finishes
    without the recipient's."""
    scripts = Scripts({"backend": notify_once("frontend", "fyi")})
    pool = api.make_pool(scripts)
    await pool.start(AGENT_KEYS)
    try:
        created = (
            await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
        ).json()
        assert await pool.wait_until_idle(timeout=30)
    finally:
        await pool.stop()

    sender = (await api.get(f"/api/tasks/{created['id']}")).json()
    call = next(c for c in sender["tool_calls"] if c["tool_name"] == "send_message")
    assert call["error"] is None
    # The tool result names the follow-up task rather than carrying a reply.
    assert call["result"]["message_id"]
    assert sender["status"] == "needs_review"


async def test_waking_can_be_turned_off(api, monkeypatch) -> None:
    monkeypatch.setattr(api.settings, "wake_on_notify", False)
    scripts = Scripts({"backend": notify_once("frontend", "fyi")})
    pool = api.make_pool(scripts)
    await pool.start(AGENT_KEYS)
    try:
        created = (
            await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
        ).json()
        assert await pool.wait_until_idle(timeout=30)
    finally:
        await pool.stop()

    assert "frontend" not in scripts.calls
    detail = (await api.get(f"/api/tasks/{created['id']}")).json()
    assert detail["tree"]["delegated_task_count"] == 0
    # The message is still recorded — it is delivery that changed, not the trace.
    assert any(m["message_type"] == "agent_to_agent" for m in detail["messages"])


# -- containment -----------------------------------------------------------


async def test_a_notification_storm_terminates(api) -> None:
    """Every agent notifies the next one, forever. Because each notification
    can now start work, an unbounded chain is an unbounded amount of work — so
    this has to stop on its own."""
    scripts = Scripts(
        {
            "backend": notify_once("frontend", "your turn"),
            "frontend": notify_once("database", "your turn"),
            "database": notify_once("devops", "your turn"),
            "devops": notify_once("backend", "your turn"),
        }
    )
    pool = api.make_pool(scripts)
    await pool.start(AGENT_KEYS)
    try:
        created = (
            await api.post("/api/tasks", json={"agent_key": "backend", "description": "start"})
        ).json()
        assert await pool.wait_until_idle(timeout=60), "the storm never settled"
    finally:
        await pool.stop()

    detail = (await api.get(f"/api/tasks/{created['id']}")).json()
    # Bounded by the tree's hop budget, not by luck.
    assert len(detail["delegation"]) <= api.settings.max_agent_hops + 1

    factory = db_module.get_session_factory()
    async with factory() as session:
        refusals = [
            c.error
            for c in (
                (
                    await session.execute(
                        select(ToolCall).where(ToolCall.tool_name == "send_message")
                    )
                )
                .scalars()
                .all()
            )
            if c.error
        ]
    assert refusals, "nothing ever refused; the chain stopped for the wrong reason"
    assert any("hop limit" in e or "cycle" in e.lower() for e in refusals)


async def test_a_direct_ping_pong_is_refused_by_cycle_detection(api) -> None:
    """A notifies B, B notifies A. B is a descendant of A in this tree, so the
    return notification is a cycle and never queues more work."""
    scripts = Scripts(
        {
            "backend": notify_once("frontend", "ping"),
            "frontend": notify_once("backend", "pong"),
        }
    )
    pool = api.make_pool(scripts)
    await pool.start(AGENT_KEYS)
    try:
        created = (
            await api.post("/api/tasks", json={"agent_key": "backend", "description": "ping"})
        ).json()
        assert await pool.wait_until_idle(timeout=30)
    finally:
        await pool.stop()

    detail = (await api.get(f"/api/tasks/{created['id']}")).json()
    assert len(detail["delegation"]) == 2, "the pong must not have created a third task"

    factory = db_module.get_session_factory()
    async with factory() as session:
        refusals = [
            c.error
            for c in (
                (
                    await session.execute(
                        select(ToolCall).where(ToolCall.tool_name == "send_message")
                    )
                )
                .scalars()
                .all()
            )
            if c.error
        ]
    assert any("cycle" in e.lower() for e in refusals)


async def test_the_hop_budget_survives_the_queue(api) -> None:
    """The in-memory context is gone by the time the worker runs a woken task.
    If the budget did not rebuild from the database, every hop would reset and
    the limit would mean nothing."""
    from app.agents.delegation import restore_context

    scripts = Scripts(
        {
            "backend": notify_once("frontend", "one"),
            "frontend": notify_once("database", "two"),
        }
    )
    pool = api.make_pool(scripts)
    await pool.start(AGENT_KEYS)
    try:
        created = (
            await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
        ).json()
        assert await pool.wait_until_idle(timeout=30)
    finally:
        await pool.stop()

    factory = db_module.get_session_factory()
    async with factory() as session:
        deepest = (
            (
                await session.execute(
                    select(Task).where(Task.parent_task_id.isnot(None)).order_by(Task.id.desc())
                )
            )
            .scalars()
            .first()
        )
        context = await restore_context(
            session, deepest, max_hops=10, deadline_seconds=600, agent_key="database"
        )

    # Two notifications happened before this task, so the budget is already spent
    # by two — not reset to zero.
    assert context.hops == 2
    assert context.root_task_id == created["id"]
    assert context.chain[0] == "backend"
    assert context.chain[-1] == "database"


async def test_restored_context_does_not_restart_the_clock(api) -> None:
    """A woken task must inherit the tree's remaining time, not a fresh budget."""
    import asyncio

    from app.agents.delegation import restore_context

    scripts = Scripts({"backend": notify_once("frontend", "fyi")})
    pool = api.make_pool(scripts)
    await pool.start(AGENT_KEYS)
    try:
        await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
        assert await pool.wait_until_idle(timeout=30)
    finally:
        await pool.stop()

    await asyncio.sleep(0.2)

    factory = db_module.get_session_factory()
    async with factory() as session:
        woken = (
            (await session.execute(select(Task).where(Task.parent_task_id.isnot(None))))
            .scalars()
            .first()
        )
        context = await restore_context(
            session, woken, max_hops=10, deadline_seconds=600, agent_key="frontend"
        )

    assert context.elapsed > 0.15, "the clock restarted instead of inheriting the tree's age"
    assert context.remaining_seconds < 600


async def test_the_conversation_is_still_recorded_in_the_trace(api) -> None:
    scripts = Scripts({"backend": notify_once("frontend", "The contract is published.")})
    pool = api.make_pool(scripts)
    await pool.start(AGENT_KEYS)
    try:
        created = (
            await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
        ).json()
        assert await pool.wait_until_idle(timeout=30)
    finally:
        await pool.stop()

    factory = db_module.get_session_factory()
    async with factory() as session:
        notes = (
            (
                await session.execute(
                    select(Message).where(Message.message_type == MessageType.AGENT_TO_AGENT)
                )
            )
            .scalars()
            .all()
        )
    assert len(notes) == 1
    assert (notes[0].from_agent, notes[0].to_agent) == ("backend", "frontend")
    assert notes[0].task_id == created["id"]
