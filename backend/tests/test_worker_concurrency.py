"""Worker pool: concurrent tasks on different agents, serialised within a lane."""

from __future__ import annotations

import asyncio
import time

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.models  # noqa: F401
from app.core import db as db_module
from app.events.bus import EventBus
from app.main import app as fastapi_app
from app.workers import queue as queue_module
from tests.api_harness import FakeClient, FakeResponse, scripted_pool, text_block

AGENT_KEYS = ["backend", "database", "devops", "frontend"]


def slow_client(delay: float):
    """A model that takes measurable time, so overlap is observable."""

    class SlowFake(FakeClient):
        async def _create(self, **kwargs):
            await asyncio.sleep(delay)
            return await FakeClient.messages.create(self, **kwargs)

    def factory(_key: str) -> FakeClient:
        client = FakeClient([FakeResponse([text_block("done")], "end_turn")], repeat_last=True)
        original = client.messages.create

        async def delayed(**kwargs):
            await asyncio.sleep(delay)
            return await original(**kwargs)

        client.messages.create = delayed  # type: ignore[method-assign]
        return client

    return factory


@pytest_asyncio.fixture
async def api(api_settings, monkeypatch):
    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_module.Base.metadata.create_all)

    bus = EventBus()
    monkeypatch.setattr("app.events.bus._bus", bus)
    monkeypatch.setattr("app.api.tasks.get_event_bus", lambda: bus)
    pool = scripted_pool(api_settings, bus, slow_client(0.20))
    monkeypatch.setattr(queue_module, "_pool", pool)
    monkeypatch.setattr("app.api.tasks.get_worker_pool", lambda: pool)
    await pool.start(AGENT_KEYS)

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.pool = pool  # type: ignore[attr-defined]
        client.bus = bus  # type: ignore[attr-defined]
        yield client

    await pool.stop()
    await db_module.dispose_engine()


async def test_four_agents_run_concurrently(api) -> None:
    """Four 0.2s tasks on four agents must overlap, not queue up behind each
    other. Serialised they would take ~0.8s; concurrent, ~0.2s."""
    started = time.perf_counter()
    for key in AGENT_KEYS:
        response = await api.post(
            "/api/tasks", json={"agent_key": key, "description": f"work for {key}"}
        )
        assert response.status_code == 201

    assert await api.pool.wait_until_idle(timeout=20)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.65, f"lanes did not run concurrently: {elapsed:.2f}s for four 0.2s tasks"

    statuses = {t["agent_key"]: t["status"] for t in (await api.get("/api/tasks")).json()}
    assert statuses == {key: "needs_review" for key in AGENT_KEYS}


async def test_tasks_for_one_agent_are_serialised(api) -> None:
    """One agent is one character at one desk. Three tasks for the same agent
    must run one after another, so ~0.6s rather than ~0.2s."""
    started = time.perf_counter()
    for i in range(3):
        await api.post("/api/tasks", json={"agent_key": "backend", "description": f"task {i}"})

    assert await api.pool.wait_until_idle(timeout=20)
    elapsed = time.perf_counter() - started

    assert elapsed >= 0.55, f"same-agent tasks overlapped: {elapsed:.2f}s for three 0.2s tasks"

    tasks = (await api.get("/api/tasks")).json()
    assert len(tasks) == 3
    assert all(t["status"] == "needs_review" for t in tasks)


async def test_http_returns_before_the_task_finishes(api) -> None:
    """The point of the worker: POST must not block on a long agent run."""
    started = time.perf_counter()
    response = await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
    post_elapsed = time.perf_counter() - started

    assert response.status_code == 201
    assert response.json()["status"] == "queued"
    assert post_elapsed < 0.15, f"POST blocked on execution ({post_elapsed:.2f}s)"

    assert await api.pool.wait_until_idle(timeout=20)


async def test_concurrent_tasks_do_not_corrupt_each_others_traces(api) -> None:
    """Each task runs in its own session; rows must not leak between them."""
    ids = []
    for key in AGENT_KEYS:
        ids.append(
            (
                await api.post(
                    "/api/tasks", json={"agent_key": key, "description": f"work for {key}"}
                )
            ).json()["id"]
        )
    assert await api.pool.wait_until_idle(timeout=20)

    for task_id, key in zip(ids, AGENT_KEYS, strict=True):
        detail = (await api.get(f"/api/tasks/{task_id}")).json()
        assert detail["agent_key"] == key
        assert all(m["task_id"] == task_id for m in detail["messages"])
        assert all(u["id"] for u in detail["usage"])
        # Every usage row belongs to this task's own agent and model.
        assert {c["agent_key"] for c in detail["tool_calls"]} <= {key}


async def test_a_failing_task_does_not_kill_its_lane(api) -> None:
    """One bad task must not take the agent offline for every later task."""
    api.pool.client_factory = lambda _k: FakeClient([FakeResponse([text_block("")], "max_tokens")])
    first = (
        await api.post("/api/tasks", json={"agent_key": "backend", "description": "boom"})
    ).json()
    assert await api.pool.wait_until_idle(timeout=20)
    assert (await api.get(f"/api/tasks/{first['id']}")).json()["status"] == "failed"

    api.pool.client_factory = lambda _k: FakeClient(
        [FakeResponse([text_block("recovered")], "end_turn")]
    )
    second = (
        await api.post("/api/tasks", json={"agent_key": "backend", "description": "again"})
    ).json()
    assert await api.pool.wait_until_idle(timeout=20)
    assert (await api.get(f"/api/tasks/{second['id']}")).json()["status"] == "needs_review"
