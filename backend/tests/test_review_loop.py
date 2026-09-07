"""Review: approval, rejection, and the re-queue loop."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.models  # noqa: F401
from app.core import db as db_module
from app.events.bus import EventBus
from app.main import app as fastapi_app
from app.workers import queue as queue_module
from tests.api_harness import FakeClient, FakeResponse, scripted_pool, text_block, tool_use_block

AGENT_KEYS = ["backend", "database", "devops", "frontend"]


class RecordingFactory:
    """Returns a fresh scripted client per run and records what it was sent.

    The point of a rejection is that the agent resumes with context, so the
    tests need to see the exact message list the second attempt received.
    """

    def __init__(self) -> None:
        self.clients: list[FakeClient] = []

    def __call__(self, _agent_key: str) -> FakeClient:
        attempt = len(self.clients) + 1
        client = FakeClient(
            [
                FakeResponse(
                    [
                        tool_use_block(
                            "write_file",
                            {"path": "out.txt", "content": f"attempt {attempt}"},
                            f"toolu_{attempt}",
                        )
                    ],
                    "tool_use",
                ),
                FakeResponse([text_block(f"Finished attempt {attempt}.")], "end_turn"),
            ]
        )
        self.clients.append(client)
        return client


@pytest_asyncio.fixture
async def api(api_settings, monkeypatch):
    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_module.Base.metadata.create_all)

    bus = EventBus()
    monkeypatch.setattr("app.events.bus._bus", bus)
    monkeypatch.setattr("app.api.tasks.get_event_bus", lambda: bus)

    factory = RecordingFactory()
    pool = scripted_pool(api_settings, bus, factory)
    monkeypatch.setattr(queue_module, "_pool", pool)
    monkeypatch.setattr("app.api.tasks.get_worker_pool", lambda: pool)
    await pool.start(AGENT_KEYS)

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.pool = pool  # type: ignore[attr-defined]
        client.bus = bus  # type: ignore[attr-defined]
        client.factory = factory  # type: ignore[attr-defined]
        yield client

    await pool.stop()
    await db_module.dispose_engine()


async def _run_to_review(api, description: str = "Build the thing") -> int:
    created = (
        await api.post("/api/tasks", json={"agent_key": "backend", "description": description})
    ).json()
    assert await api.pool.wait_until_idle(timeout=15)
    assert (await api.get(f"/api/tasks/{created['id']}")).json()["status"] == "needs_review"
    return created["id"]


# -- approval --------------------------------------------------------------


async def test_approval_marks_the_task_done(api) -> None:
    task_id = await _run_to_review(api)

    response = await api.post(f"/api/tasks/{task_id}/review", json={"decision": "approve"})

    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert (await api.get(f"/api/tasks/{task_id}")).json()["status"] == "done"


async def test_approval_does_not_re_run_the_agent(api) -> None:
    task_id = await _run_to_review(api)
    runs_before = len(api.factory.clients)

    await api.post(f"/api/tasks/{task_id}/review", json={"decision": "approve"})
    assert await api.pool.wait_until_idle(timeout=10)

    assert len(api.factory.clients) == runs_before


async def test_a_done_task_cannot_be_reviewed_again(api) -> None:
    task_id = await _run_to_review(api)
    await api.post(f"/api/tasks/{task_id}/review", json={"decision": "approve"})

    again = await api.post(f"/api/tasks/{task_id}/review", json={"decision": "approve"})
    assert again.status_code == 409
    assert "needs_review" in again.json()["detail"]


# -- rejection -------------------------------------------------------------


async def test_rejection_requires_feedback(api) -> None:
    task_id = await _run_to_review(api)

    response = await api.post(f"/api/tasks/{task_id}/review", json={"decision": "reject"})

    assert response.status_code == 422
    assert "feedback" in response.json()["detail"]
    assert (await api.get(f"/api/tasks/{task_id}")).json()["status"] == "needs_review"


async def test_rejection_requeues_and_reruns_the_agent(api) -> None:
    task_id = await _run_to_review(api)
    runs_before = len(api.factory.clients)

    response = await api.post(
        f"/api/tasks/{task_id}/review",
        json={"decision": "reject", "feedback": "The error handling is missing."},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["attempt"] == 2

    assert await api.pool.wait_until_idle(timeout=15)
    assert len(api.factory.clients) == runs_before + 1

    detail = (await api.get(f"/api/tasks/{task_id}")).json()
    assert detail["status"] == "needs_review"
    assert detail["attempt"] == 2
    assert detail["review_feedback"] == "The error handling is missing."


async def test_feedback_is_appended_as_a_user_message(api) -> None:
    task_id = await _run_to_review(api)
    feedback = "Add validation on the request body."

    await api.post(
        f"/api/tasks/{task_id}/review", json={"decision": "reject", "feedback": feedback}
    )
    assert await api.pool.wait_until_idle(timeout=15)

    messages = (await api.get(f"/api/tasks/{task_id}")).json()["messages"]
    user_messages = [m for m in messages if m["message_type"] == "user"]
    assert any(m["content"] == feedback for m in user_messages)


async def test_the_rerun_sees_the_prior_conversation_and_the_feedback(api) -> None:
    """The whole point of a rejection: the agent resumes with what it already
    did plus the criticism, rather than starting over blind."""
    task_id = await _run_to_review(api)
    feedback = "You forgot the error handling."

    await api.post(
        f"/api/tasks/{task_id}/review", json={"decision": "reject", "feedback": feedback}
    )
    assert await api.pool.wait_until_idle(timeout=15)

    second_run = api.factory.clients[1]
    sent = second_run.calls[0]["messages"]

    assert len(sent) > 1, "the second attempt started from scratch"
    assert sent[-1]["role"] == "user", "the API requires the last turn to be the user's"
    assert sent[-1]["content"] == feedback
    # The prior attempt's own work is still in context.
    assert any(m["role"] == "assistant" for m in sent)


async def test_rejection_can_repeat(api) -> None:
    task_id = await _run_to_review(api)

    for expected_attempt in (2, 3, 4):
        response = await api.post(
            f"/api/tasks/{task_id}/review",
            json={"decision": "reject", "feedback": f"Round {expected_attempt}: try again."},
        )
        assert response.status_code == 200
        assert response.json()["attempt"] == expected_attempt
        assert await api.pool.wait_until_idle(timeout=15)

    detail = (await api.get(f"/api/tasks/{task_id}")).json()
    assert detail["attempt"] == 4
    assert detail["status"] == "needs_review"


async def test_approval_after_rejection_closes_the_loop(api) -> None:
    task_id = await _run_to_review(api)
    await api.post(
        f"/api/tasks/{task_id}/review", json={"decision": "reject", "feedback": "Not yet."}
    )
    assert await api.pool.wait_until_idle(timeout=15)

    final = await api.post(f"/api/tasks/{task_id}/review", json={"decision": "approve"})
    assert final.status_code == 200
    assert final.json()["status"] == "done"
    assert final.json()["attempt"] == 2


# -- guards ----------------------------------------------------------------


async def test_reviewing_a_queued_task_is_rejected(api) -> None:
    created = (
        await api.post("/api/tasks", json={"agent_key": "backend", "description": "x"})
    ).json()
    # Race the worker: whether it is queued or already running, neither is
    # reviewable, and the API must say so rather than corrupting the lifecycle.
    response = await api.post(f"/api/tasks/{created['id']}/review", json={"decision": "approve"})
    if response.status_code == 200:  # pragma: no cover - lost the race
        pytest.skip("worker finished before the review call")
    assert response.status_code == 409
    assert await api.pool.wait_until_idle(timeout=15)


async def test_reviewing_a_missing_task_is_404(api) -> None:
    response = await api.post("/api/tasks/9999/review", json={"decision": "approve"})
    assert response.status_code == 404


@pytest.mark.parametrize("payload", [{"decision": "maybe"}, {}, {"feedback": "x"}])
async def test_invalid_review_payloads_are_rejected(api, payload: dict) -> None:
    task_id = await _run_to_review(api)
    assert (await api.post(f"/api/tasks/{task_id}/review", json=payload)).status_code == 422


async def test_rejection_emits_a_status_change_event(api) -> None:
    task_id = await _run_to_review(api)
    before = len(api.bus.history())

    await api.post(
        f"/api/tasks/{task_id}/review", json={"decision": "reject", "feedback": "again please"}
    )

    new_events = api.bus.history()[before:]
    kinds = [e.type for e in new_events]
    assert "task.status_changed" in kinds
    assert "message.created" in kinds
