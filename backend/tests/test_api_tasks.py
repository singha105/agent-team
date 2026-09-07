"""Task REST API, driven through the real app with a scripted model."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.models  # noqa: F401  registers tables
from app.core import db as db_module
from app.events.bus import EventBus
from app.main import app as fastapi_app
from app.workers import queue as queue_module
from tests.api_harness import finishes_immediately, scripted_pool, writes_then_finishes

AGENT_KEYS = ["backend", "database", "devops", "frontend"]


@pytest_asyncio.fixture
async def api(api_settings, monkeypatch):
    """The real app, a temp database, a real bus and worker pool, a fake model."""
    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_module.Base.metadata.create_all)

    bus = EventBus()
    monkeypatch.setattr("app.events.bus._bus", bus)
    monkeypatch.setattr("app.api.tasks.get_event_bus", lambda: bus)

    pool = scripted_pool(api_settings, bus, finishes_immediately())
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


# -- creation --------------------------------------------------------------


async def test_create_task_returns_immediately_with_an_id(api) -> None:
    response = await api.post(
        "/api/tasks", json={"agent_key": "backend", "description": "Build an endpoint"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["agent_key"] == "backend"
    assert body["status"] == "queued"
    assert body["created_by"] == "human"
    assert body["attempt"] == 1


async def test_create_derives_a_title_from_a_long_description(api) -> None:
    long_description = "Refactor " + "the service layer " * 20
    response = await api.post(
        "/api/tasks", json={"agent_key": "backend", "description": long_description}
    )
    title = response.json()["title"]
    assert len(title) <= 120
    assert title.endswith("…")


async def test_explicit_title_is_kept(api) -> None:
    response = await api.post(
        "/api/tasks",
        json={"agent_key": "backend", "description": "d", "title": "Ship the books API"},
    )
    assert response.json()["title"] == "Ship the books API"


async def test_unknown_agent_is_404_not_500(api) -> None:
    response = await api.post("/api/tasks", json={"agent_key": "nonexistent", "description": "x"})
    assert response.status_code == 404
    assert "nonexistent" in response.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"agent_key": "backend"},
        {"description": "x"},
        {"agent_key": "backend", "description": ""},
        {"agent_key": "", "description": "x"},
        {"agent_key": "backend", "description": "x", "unexpected": 1},
    ],
)
async def test_invalid_payloads_are_rejected(api, payload: dict) -> None:
    assert (await api.post("/api/tasks", json=payload)).status_code == 422


async def test_task_runs_in_the_background_and_reaches_needs_review(api) -> None:
    created = (
        await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
    ).json()

    assert await api.pool.wait_until_idle(timeout=10)

    detail = (await api.get(f"/api/tasks/{created['id']}")).json()
    assert detail["status"] == "needs_review"


# -- retrieval -------------------------------------------------------------


async def test_get_task_includes_full_history(api) -> None:
    api.pool.client_factory = writes_then_finishes()
    created = (
        await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
    ).json()
    assert await api.pool.wait_until_idle(timeout=10)

    detail = (await api.get(f"/api/tasks/{created['id']}")).json()
    assert len(detail["messages"]) >= 4
    assert len(detail["tool_calls"]) == 1
    assert detail["tool_calls"][0]["tool_name"] == "write_file"
    assert len(detail["usage"]) == 2
    assert detail["total_tokens"] > 0
    assert detail["total_cost_usd"] > 0


async def test_get_missing_task_is_404(api) -> None:
    assert (await api.get("/api/tasks/9999")).status_code == 404


async def test_list_tasks_filters_by_agent_and_status(api) -> None:
    for key in ("backend", "frontend"):
        await api.post("/api/tasks", json={"agent_key": key, "description": f"work for {key}"})
    assert await api.pool.wait_until_idle(timeout=15)

    everything = (await api.get("/api/tasks")).json()
    assert len(everything) == 2

    backend_only = (await api.get("/api/tasks", params={"agent_key": "backend"})).json()
    assert [t["agent_key"] for t in backend_only] == ["backend"]

    reviewable = (await api.get("/api/tasks", params={"status": "needs_review"})).json()
    assert len(reviewable) == 2
    assert all(t["status"] == "needs_review" for t in reviewable)


# -- trace -----------------------------------------------------------------


async def test_trace_interleaves_kinds_in_time_order(api) -> None:
    api.pool.client_factory = writes_then_finishes()
    created = (
        await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
    ).json()
    assert await api.pool.wait_until_idle(timeout=10)

    trace = (await api.get(f"/api/tasks/{created['id']}/trace")).json()

    kinds = {e["kind"] for e in trace["entries"]}
    assert kinds == {"message", "tool_call", "usage"}

    # Ordered: the trace only reconstructs a run if the steps are in sequence.
    ats = [(e["at"], e["ref_id"]) for e in trace["entries"]]
    assert ats == sorted(ats)
    assert trace["total_cost_usd"] > 0
    assert trace["attempt"] == 1


async def test_trace_for_missing_task_is_404(api) -> None:
    assert (await api.get("/api/tasks/9999/trace")).status_code == 404


# -- roster ----------------------------------------------------------------


async def test_agents_endpoint_returns_the_whole_roster(api) -> None:
    roster = (await api.get("/api/agents")).json()
    assert [a["key"] for a in roster] == AGENT_KEYS

    by_key = {a["key"]: a for a in roster}
    # Model assignment per spec section 1.
    assert by_key["backend"]["model"] == "claude-opus-5"
    assert by_key["devops"]["model"] == "claude-opus-5"
    assert by_key["frontend"]["model"] == "claude-sonnet-5"
    assert by_key["database"]["model"] == "claude-sonnet-5"

    for agent in roster:
        assert agent["display_name"]
        assert agent["bio"], f"{agent['key']} needs a character bio for the UI"
        assert agent["personality"]
        assert agent["owns"]
        assert agent["status"] in {
            "idle",
            "thinking",
            "working",
            "waiting_on_human",
            "blocked",
            "error",
        }


async def test_agent_status_reflects_a_finished_run(api) -> None:
    await api.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
    assert await api.pool.wait_until_idle(timeout=10)

    roster = {a["key"]: a for a in (await api.get("/api/agents")).json()}
    assert roster["backend"]["status"] == "waiting_on_human"
    # An agent that has never been dispatched to stays idle.
    assert roster["devops"]["status"] == "idle"
