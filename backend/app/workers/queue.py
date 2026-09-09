"""Background task execution.

One asyncio queue and one worker coroutine per agent. That shape is deliberate:
it serialises work within an agent's lane — an agent is one character at one
desk and cannot sensibly do two things at once — while letting different agents
run concurrently, which is the whole point of a team.

No Celery, no broker: the spec rules that out for Phase 2, and a single process
with asyncio is sufficient for four agents.

Each task runs in its own DB session. Sharing a session across concurrently
running tasks would interleave flushes on a connection that is not
concurrency-safe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from sqlalchemy import select

from app.agents.config_loader import AgentConfigError, get_agent_config
from app.agents.runtime import AgentRuntime
from app.core.config import Settings, get_settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.events.bus import EventBus, get_event_bus
from app.models import Task

log = get_logger(__name__)

SHUTDOWN_GRACE_SECONDS = 30


@dataclass(frozen=True)
class QueuedTask:
    task_id: int
    agent_key: str


class TaskWorkerPool:
    """Runs queued tasks in the background, one lane per agent."""

    def __init__(
        self,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        client_factory: Callable[[str], AsyncAnthropic] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.bus = bus or get_event_bus()
        # Test seam. In production this stays None and AgentRuntime builds a
        # real AsyncAnthropic client from the configured key.
        self.client_factory = client_factory
        self._queues: dict[str, asyncio.Queue[QueuedTask | None]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._running = False
        # Lets tests await completion without polling the database.
        self._idle = asyncio.Event()
        self._idle.set()
        self._in_flight = 0
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def start(self, agent_keys: list[str]) -> None:
        self._running = True
        for key in agent_keys:
            if key not in self._workers:
                self._queues[key] = asyncio.Queue()
                self._workers[key] = asyncio.create_task(
                    self._worker(key), name=f"agentteam-worker-{key}"
                )
        log.info("worker pool started for %s", ", ".join(sorted(self._workers)))

    async def stop(self) -> None:
        """Signal every lane to finish and wait, bounded."""
        self._running = False
        for queue in self._queues.values():
            queue.put_nowait(None)  # sentinel
        if self._workers:
            await asyncio.wait(list(self._workers.values()), timeout=SHUTDOWN_GRACE_SECONDS)
            for worker in self._workers.values():
                if not worker.done():
                    worker.cancel()
        self._workers.clear()
        self._queues.clear()
        log.info("worker pool stopped")

    # -- submission --------------------------------------------------------

    async def submit(self, task_id: int, agent_key: str) -> None:
        """Enqueue a task. Returns immediately — the HTTP caller does not wait."""
        if agent_key not in self._queues:
            # An agent with no lane yet (added to config after startup).
            self._queues[agent_key] = asyncio.Queue()
            self._workers[agent_key] = asyncio.create_task(
                self._worker(agent_key), name=f"agentteam-worker-{agent_key}"
            )
        async with self._lock:
            self._in_flight += 1
            self._idle.clear()
        await self._queues[agent_key].put(QueuedTask(task_id=task_id, agent_key=agent_key))

    async def wait_until_idle(self, timeout: float = 60.0) -> bool:  # noqa: ASYNC109
        """Block until every queued task has finished. Test and shutdown hook."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    @property
    def in_flight(self) -> int:
        return self._in_flight

    # -- execution ---------------------------------------------------------

    async def _worker(self, agent_key: str) -> None:
        queue = self._queues[agent_key]
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                return
            try:
                await self._run_one(item)
            except Exception:  # noqa: BLE001 - a failed task must not kill the lane
                log.exception("worker for %s failed on task %s", agent_key, item.task_id)
            finally:
                queue.task_done()
                async with self._lock:
                    self._in_flight -= 1
                    if self._in_flight == 0:
                        self._idle.set()

    async def _run_one(self, item: QueuedTask) -> None:
        """Execute one task in its own session."""
        async with session_scope() as session:
            task = (
                await session.execute(select(Task).where(Task.id == item.task_id))
            ).scalar_one_or_none()
            if task is None:
                log.warning("task %s vanished before it ran", item.task_id)
                return

            try:
                config = get_agent_config(item.agent_key)
            except AgentConfigError as exc:
                # Config changed under us between dispatch and execution.
                from app.agents.lifecycle import validate_transition
                from app.models import TaskStatus

                validate_transition(task.status, TaskStatus.FAILED)
                task.status = TaskStatus.FAILED
                task.halt_reason = str(exc)
                return

            client = self.client_factory(item.agent_key) if self.client_factory else None
            runtime = AgentRuntime(
                config,
                session,
                self.settings,
                client=client,
                bus=self.bus,
                # Passed down so a delegated run builds its own client for the
                # agent it belongs to, rather than reusing the caller's.
                client_for=self.client_factory,
                # Lets a notified agent be woken with a queued follow-up task.
                wake=self.submit,
            )
            result = await runtime.run(task)
            log.info(
                "task %s finished as %s (%s)",
                item.task_id,
                result.status,
                result.budget.get("iterations"),
            )


_pool: TaskWorkerPool | None = None


def get_worker_pool() -> TaskWorkerPool:
    global _pool
    if _pool is None:
        _pool = TaskWorkerPool()
    return _pool


def reset_worker_pool() -> None:
    """Test hook."""
    global _pool
    _pool = None
