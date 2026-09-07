"""WebSocket event stream.

Clients connect to /ws and receive every event as JSON. The socket is a
notification channel: events carry ids and short previews, and the client
fetches full bodies over REST. That keeps one large write_file from stalling
the stream for every other subscriber.

Each connection gets its own bounded queue. A client that stops reading has
events dropped from its own queue and is told how many; it never applies
backpressure to the agent loop.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.events.bus import get_event_bus
from app.events.schemas import EVENT_TYPES

log = get_logger(__name__)

router = APIRouter(tags=["events"])


@router.websocket("/ws")
async def event_stream(websocket: WebSocket, since_seq: int = Query(default=0, ge=0)) -> None:
    """Stream events.

    `since_seq` replays buffered events after that sequence number, so a client
    that reconnects can close the gap instead of silently missing a run.
    """
    await websocket.accept()
    bus = get_event_bus()

    async with bus.subscription() as subscriber:
        await websocket.send_json(
            {
                "type": "stream.ready",
                "event_types": list(EVENT_TYPES),
                "since_seq": since_seq,
            }
        )

        for buffered in bus.history(since_seq=since_seq):
            await websocket.send_text(buffered.model_dump_json())

        # A concurrent reader lets a client-side close be noticed promptly
        # instead of only when the next event happens to arrive.
        async def _drain_incoming() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except Exception:  # noqa: BLE001 - disconnect ends the task
                return

        reader = asyncio.create_task(_drain_incoming())
        pending_get: asyncio.Task | None = None
        try:
            while True:
                pending_get = asyncio.create_task(subscriber.get())
                done, _ = await asyncio.wait(
                    [pending_get, reader], return_when=asyncio.FIRST_COMPLETED
                )
                if reader in done:
                    break
                event = pending_get.result()
                pending_get = None
                await websocket.send_text(event.model_dump_json())
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("websocket stream failed")
        finally:
            # Both tasks must be cancelled explicitly. Leaving the outstanding
            # subscriber.get() pending leaks a task per disconnected client and
            # produces "Task was destroyed but it is pending" on shutdown.
            for task in (reader, pending_get):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            if subscriber.dropped:
                log.warning("client disconnected after dropping %d events", subscriber.dropped)
