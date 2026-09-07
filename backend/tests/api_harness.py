"""Shared harness for the Phase 2 API, worker and WebSocket tests.

Wires the real FastAPI app to a temporary database and a scripted model, so the
tests exercise the actual routers, the actual worker pool and the actual event
bus — only the Messages API call is replaced.
"""

from __future__ import annotations

from collections.abc import Callable

from app.events.bus import EventBus
from app.workers.queue import TaskWorkerPool
from tests.fakes import FakeClient, FakeResponse, text_block, tool_use_block

__all__ = [
    "FakeClient",
    "FakeResponse",
    "finishes_immediately",
    "scripted_pool",
    "text_block",
    "tool_use_block",
    "writes_then_finishes",
]


def finishes_immediately(summary: str = "Done.") -> Callable[[str], FakeClient]:
    """Every agent replies once and stops."""
    return lambda _key: FakeClient([FakeResponse([text_block(summary)], "end_turn")])


def writes_then_finishes(path: str = "out.txt", body: str = "x") -> Callable[[str], FakeClient]:
    """Each agent writes one file in its own lane, then reports."""

    def factory(agent_key: str) -> FakeClient:
        return FakeClient(
            [
                FakeResponse(
                    [
                        tool_use_block(
                            "write_file", {"path": f"{agent_key}/{path}", "content": body}
                        )
                    ],
                    "tool_use",
                ),
                FakeResponse([text_block(f"{agent_key} wrote {agent_key}/{path}")], "end_turn"),
            ]
        )

    return factory


def scripted_pool(
    settings, bus: EventBus, client_factory: Callable[[str], FakeClient]
) -> TaskWorkerPool:
    return TaskWorkerPool(settings=settings, bus=bus, client_factory=client_factory)
