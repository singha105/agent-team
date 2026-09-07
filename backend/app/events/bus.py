"""In-process event bus.

One publisher fan-out to many WebSocket subscribers. No broker: Phase 2 runs a
single process, and the spec explicitly rules out Celery-style infrastructure.

Two properties the WebSocket contract depends on:

  Ordering — `seq` is assigned under a lock at publish time and each subscriber
  has its own FIFO queue, so every subscriber observes events in the same
  order. Clients can detect a gap by watching for a skipped `seq`.

  Isolation — a slow or dead subscriber must not stall the agent loop. Queues
  are bounded; when one fills, the oldest event is dropped for that subscriber
  only and the drop is counted, rather than applying backpressure to the
  producer or growing without limit.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.logging import get_logger
from app.events.schemas import BaseEvent

log = get_logger(__name__)

DEFAULT_QUEUE_SIZE = 512


class Subscriber:
    """One client's view of the stream."""

    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        self.queue: asyncio.Queue[BaseEvent] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def offer(self, event: BaseEvent) -> None:
        """Non-blocking put. Drops the oldest event if this subscriber is full."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover - race
                pass
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.warning("subscriber is falling behind; dropped %d events", self.dropped)

    async def get(self) -> BaseEvent:
        return await self.queue.get()


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._seq = 0
        self._lock = asyncio.Lock()
        # History lets a late subscriber and the tests inspect what was emitted.
        self._history: list[BaseEvent] = []
        self._history_limit = 1000

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: BaseEvent) -> BaseEvent:
        """Stamp the event with a sequence number and fan it out."""
        async with self._lock:
            self._seq += 1
            event.seq = self._seq
            self._history.append(event)
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]
            targets = list(self._subscribers)

        for subscriber in targets:
            subscriber.offer(event)
        return event

    def subscribe(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> Subscriber:
        subscriber = Subscriber(maxsize=maxsize)
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    @asynccontextmanager
    async def subscription(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> AsyncIterator[Subscriber]:
        subscriber = self.subscribe(maxsize=maxsize)
        try:
            yield subscriber
        finally:
            self.unsubscribe(subscriber)

    def history(self, since_seq: int = 0) -> list[BaseEvent]:
        return [e for e in self._history if e.seq > since_seq]

    def reset(self) -> None:
        """Test hook. Never called in production paths."""
        self._subscribers.clear()
        self._history.clear()
        self._seq = 0


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
