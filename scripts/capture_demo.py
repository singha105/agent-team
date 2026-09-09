"""Capture a run as a replayable fixture.

Produces frontend/src/demo/fixture.json: the ordered event stream plus the REST
snapshots the UI would fetch. `npm run demo` replays it with no backend at all,
so a reviewer with Node and no API key sees the real thing.

The fixture is a recording of an actual run, not a hand-written mock. Whatever
the agents really did — including anything that went wrong — is what plays back.

    python scripts/capture_demo.py \
        --task "Build a URL shortener: API, database, and a minimal web UI"

By default it captures against the scripted demo backend, which needs no key.
Pass --live to capture a real run instead; that spends money.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUTPUT = REPO / "frontend" / "src" / "demo" / "fixture.json"


async def capture(
    base_url: str, agent: str, description: str, settle_seconds: float
) -> dict[str, Any]:
    import httpx
    import websockets

    events: list[dict[str, Any]] = []

    async with websockets.connect(base_url.replace("http", "ws") + "/ws") as socket:
        hello = json.loads(await socket.recv())
        print(f"  connected; {len(hello.get('event_types', []))} event types advertised")

        async def reader() -> None:
            try:
                while True:
                    frame = await asyncio.wait_for(socket.recv(), timeout=settle_seconds)
                    payload = json.loads(frame)
                    if payload.get("type") != "stream.ready":
                        events.append(payload)
            except Exception:
                return

        reading = asyncio.create_task(reader())

        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            created = await http.post(
                "/api/tasks", json={"agent_key": agent, "description": description}
            )
            created.raise_for_status()
            root_id = created.json()["id"]
            print(f"  task {root_id} dispatched to {agent}")

            # Wait for quiet rather than for a status: a delegation tree finishes
            # when nothing has happened for a while, not when the root task
            # changes state — children can still be running.
            last_count = -1
            quiet_for = 0.0
            while quiet_for < settle_seconds:
                await asyncio.sleep(1.0)
                if len(events) == last_count:
                    quiet_for += 1.0
                else:
                    quiet_for = 0.0
                    last_count = len(events)
                    print(f"    {len(events)} events…", end="\r", flush=True)

            print(f"\n  settled after {len(events)} events")
            reading.cancel()

            agents = (await http.get("/api/agents")).json()
            tasks = (await http.get("/api/tasks", params={"limit": 200})).json()
            details = {}
            traces = {}
            for task in tasks:
                details[str(task["id"])] = (await http.get(f"/api/tasks/{task['id']}")).json()
                traces[str(task["id"])] = (await http.get(f"/api/tasks/{task['id']}/trace")).json()

    return {
        "version": 1,
        "description": description,
        "root_task_id": root_id,
        "agents": agents,
        "tasks": tasks,
        "task_detail": details,
        "traces": traces,
        "events": events,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--agent", default="backend")
    parser.add_argument(
        "--task",
        default="Build a URL shortener: API, database, and a minimal web UI",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=6.0,
        help="seconds of silence that count as the run being finished",
    )
    args = parser.parse_args()

    print(f"capturing from {args.url}")
    fixture = asyncio.run(capture(args.url, args.agent, args.task, args.settle))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(fixture, indent=1), encoding="utf-8")

    size_kb = OUTPUT.stat().st_size / 1024
    print(f"\nwrote {OUTPUT.relative_to(REPO)}  ({size_kb:.0f} KB)")
    print(f"  {len(fixture['events'])} events")
    print(f"  {len(fixture['tasks'])} tasks across {len(fixture['agents'])} agents")
    by_type: dict[str, int] = {}
    for event in fixture["events"]:
        by_type[event["type"]] = by_type.get(event["type"], 0) + 1
    for name in sorted(by_type):
        print(f"    {name:<24}{by_type[name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
