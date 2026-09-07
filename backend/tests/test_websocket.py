"""WebSocket event stream: delivery, ordering and replay."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.models  # noqa: F401
from app.core import db as db_module
from app.events.bus import EventBus
from app.events.schemas import EVENT_TYPES, TaskCreated
from app.main import app as fastapi_app
from app.workers import queue as queue_module
from tests.api_harness import scripted_pool, writes_then_finishes

AGENT_KEYS = ["backend", "database", "devops", "frontend"]


@pytest.fixture
def ws_client(api_settings, monkeypatch):
    """The real app over a sync TestClient, so websocket_connect is usable.

    The bus and worker pool are swapped for test instances before the lifespan
    runs, so the app under test is otherwise untouched.
    """
    import asyncio

    async def _create_schema() -> None:
        engine = db_module.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(db_module.Base.metadata.create_all)
        await db_module.dispose_engine()

    asyncio.run(_create_schema())

    bus = EventBus()
    monkeypatch.setattr("app.events.bus._bus", bus)
    monkeypatch.setattr("app.api.tasks.get_event_bus", lambda: bus)
    monkeypatch.setattr("app.api.ws.get_event_bus", lambda: bus)

    pool = scripted_pool(api_settings, bus, writes_then_finishes())
    monkeypatch.setattr(queue_module, "_pool", pool)
    monkeypatch.setattr(queue_module, "get_worker_pool", lambda: pool)
    monkeypatch.setattr("app.api.tasks.get_worker_pool", lambda: pool)

    with TestClient(fastapi_app) as client:
        client.bus = bus  # type: ignore[attr-defined]
        client.pool = pool  # type: ignore[attr-defined]
        yield client


def _drain(ws, *, until_type: str, limit: int = 200) -> list[dict]:
    """Read events until one of `until_type` arrives."""
    received: list[dict] = []
    for _ in range(limit):
        payload = ws.receive_json()
        if payload.get("type") == "stream.ready":
            continue
        received.append(payload)
        if payload["type"] == until_type:
            break
    return received


def test_handshake_advertises_the_event_types(ws_client) -> None:
    with ws_client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "stream.ready"
        assert set(hello["event_types"]) == set(EVENT_TYPES)


def test_events_arrive_for_a_real_run(ws_client) -> None:
    with ws_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "stream.ready"

        response = ws_client.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
        assert response.status_code == 201
        task_id = response.json()["id"]

        events = _drain(ws, until_type="task.status_changed")
        seen = [e["type"] for e in events]

        assert "task.created" in seen
        assert all(e.get("task_id") in (task_id, None) for e in events)


def test_sequence_numbers_are_strictly_increasing(ws_client) -> None:
    """A client detects a gap by watching seq, so it must never repeat or
    go backwards."""
    with ws_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws_client.post("/api/tasks", json={"agent_key": "backend", "description": "go"})

        events = []
        for _ in range(40):
            events.append(ws.receive_json())
            if events[-1]["type"] == "task.status_changed" and events[-1]["status"] in (
                "needs_review",
                "failed",
            ):
                break

        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs), "sequence numbers must not repeat"
        assert seqs[0] >= 1


def test_lifecycle_events_arrive_in_causal_order(ws_client) -> None:
    """A tool cannot finish before it started, and a task cannot change status
    before it was created."""
    with ws_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws_client.post("/api/tasks", json={"agent_key": "backend", "description": "go"})

        events = []
        for _ in range(60):
            events.append(ws.receive_json())
            if events[-1]["type"] == "task.status_changed" and events[-1]["status"] == (
                "needs_review"
            ):
                break

        order = [e["type"] for e in events]
        assert order.index("task.created") < order.index("task.status_changed")

        if "tool.started" in order:
            started = [e for e in events if e["type"] == "tool.started"]
            finished = [e for e in events if e["type"] == "tool.finished"]
            assert order.index("tool.started") < order.index("tool.finished")
            # Every finish refers to a call that started.
            assert {f["tool_call_id"] for f in finished} <= {s["tool_call_id"] for s in started}


def test_every_declared_event_type_is_emitted_by_a_real_run(ws_client) -> None:
    """The schemas are the frontend's contract; an event type nothing ever
    emits is a lie in the generated TypeScript."""
    with ws_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws_client.post("/api/tasks", json={"agent_key": "backend", "description": "go"})

        seen: set[str] = set()
        for _ in range(80):
            payload = ws.receive_json()
            seen.add(payload["type"])
            if payload["type"] == "task.status_changed" and payload["status"] == "needs_review":
                break

        expected = {
            "task.created",
            "task.status_changed",
            "agent.status_changed",
            "message.created",
            "tool.started",
            "tool.finished",
            "usage.updated",
        }
        assert expected <= seen, f"never emitted: {sorted(expected - seen)}"


def test_two_clients_observe_the_same_order(ws_client) -> None:
    with (
        ws_client.websocket_connect("/ws") as first,
        ws_client.websocket_connect("/ws") as second,
    ):
        first.receive_json()
        second.receive_json()
        ws_client.post("/api/tasks", json={"agent_key": "backend", "description": "go"})

        def collect(ws) -> list[int]:
            seqs = []
            for _ in range(30):
                payload = ws.receive_json()
                seqs.append(payload["seq"])
                if payload["type"] == "task.status_changed" and payload["status"] == (
                    "needs_review"
                ):
                    break
            return seqs

        assert collect(first) == collect(second)


def test_since_seq_replays_missed_events(ws_client) -> None:
    """A client that reconnects must be able to close the gap rather than
    silently miss a run."""
    ws_client.post("/api/tasks", json={"agent_key": "backend", "description": "go"})
    ws_client.pool  # noqa: B018 - keep the reference explicit for readability

    with ws_client.websocket_connect("/ws?since_seq=0") as ws:
        assert ws.receive_json()["type"] == "stream.ready"
        replayed = ws.receive_json()
        assert replayed["seq"] >= 1
        assert replayed["type"] in EVENT_TYPES


async def test_bus_drops_for_a_slow_subscriber_without_blocking() -> None:
    """A stalled client must never apply backpressure to the agent loop."""
    bus = EventBus()
    slow = bus.subscribe(maxsize=3)

    for i in range(50):
        await bus.publish(
            TaskCreated(
                task_id=i, agent_key="backend", title="t", status="queued", created_by="human"
            )
        )

    assert slow.queue.qsize() == 3
    assert slow.dropped == 47
    # The newest events survive; a stalled client resumes with current state.
    newest = slow.queue.get_nowait()
    assert newest.task_id >= 40


async def test_unsubscribed_clients_stop_receiving() -> None:
    bus = EventBus()
    async with bus.subscription() as sub:
        await bus.publish(
            TaskCreated(
                task_id=1, agent_key="backend", title="t", status="queued", created_by="human"
            )
        )
        assert sub.queue.qsize() == 1
    assert bus.subscriber_count == 0

    await bus.publish(
        TaskCreated(task_id=2, agent_key="backend", title="t", status="queued", created_by="human")
    )
    assert sub.queue.qsize() == 1, "an unsubscribed client must receive nothing further"
