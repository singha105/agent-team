"""Run the real backend against a scripted team, with no API key.

Everything is real except the model: the actual routers, worker pool, event bus,
message bus, sandbox and database. Only `client.messages.create` is scripted.

It exists for two reasons. The UI needs a live room to develop against, and
watching four agents collaborate should not require spending money — or an API
key, which this machine does not have.

    python scripts/demo_server.py [--port 8000] [--loop]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "backend" / "tests"))

import os  # noqa: E402

WORKDIR = Path(tempfile.mkdtemp(prefix="agentteam-demo-"))
(WORKDIR / "workspace").mkdir()
(WORKDIR / "data").mkdir()
shutil.copytree(REPO / "config" / "teams" / "software", WORKDIR / "agents")

os.environ.update(
    AGENTTEAM_WORKSPACE_ROOT=str(WORKDIR / "workspace"),
    AGENTTEAM_DB_PATH=str(WORKDIR / "data" / "demo.db"),
    AGENTTEAM_AGENT_CONFIG_DIR=str(WORKDIR / "agents"),
    AGENTTEAM_SANDBOX_MODE="subprocess",
    AGENTTEAM_LOG_LEVEL="INFO",
)

import uvicorn  # noqa: E402
from fakes import FakeClient, FakeResponse, text_block, tool_use_block  # noqa: E402

from app.core import db as dbm  # noqa: E402
from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

SCHEMA = (
    "CREATE TABLE books (\n"
    "  id INTEGER PRIMARY KEY,\n"
    "  title TEXT NOT NULL,\n"
    "  author TEXT NOT NULL,\n"
    "  year INTEGER NOT NULL,\n"
    "  isbn TEXT UNIQUE\n"
    ");\n"
    "CREATE INDEX idx_books_search ON books(title, author);\n\n"
    "Search is a LIKE across title and author, covered by idx_books_search."
)

CONTRACT = (
    "GET  /books?q=<term>  -> 200 list[Book]   search across title and author\n"
    "GET  /books/{id}      -> 200 Book | 404 {detail}\n"
    "POST /books           -> 201 Book | 422 validation error\n\n"
    "Book = {id:int, title:str, author:str, year:int, isbn:str|null}"
)


def _slow(client: FakeClient, delay: float) -> FakeClient:
    """Pace the scripted model so the room animates at a watchable speed."""
    original = client.messages.create

    async def paced(**kwargs):
        # noqa: S311 — jitter for a demo's pacing, not a security decision.
        await asyncio.sleep(delay + random.random() * delay)  # noqa: S311
        return await original(**kwargs)

    client.messages.create = paced  # type: ignore[method-assign]
    return client


def build_scripts(delay: float):
    scripts = {
        "backend": [
            FakeResponse(
                [
                    tool_use_block(
                        "ask_agent",
                        {
                            "to_agent": "database",
                            "question": "I'm building a REST API for a book library with "
                            "search. What schema should I build against?",
                        },
                        "ask1",
                    )
                ],
                "tool_use",
                3200,
                260,
            ),
            FakeResponse(
                [
                    tool_use_block(
                        "write_file",
                        {
                            "path": "api/books.py",
                            "content": "from fastapi import FastAPI, Query\n"
                            "from pydantic import BaseModel\n\napp = FastAPI()\n",
                        },
                        "w1",
                    )
                ],
                "tool_use",
                4100,
                720,
            ),
            FakeResponse(
                [
                    tool_use_block(
                        "append_project_context",
                        {"heading": "API contract: book library", "body": CONTRACT},
                        "p1",
                    )
                ],
                "tool_use",
                4600,
                380,
            ),
            FakeResponse(
                [
                    tool_use_block(
                        "send_message",
                        {
                            "to_agent": "frontend",
                            "content": "The book library API contract is published in "
                            "PROJECT.md. GET /books?q= returns list[Book].",
                        },
                        "n1",
                    )
                ],
                "tool_use",
                4900,
                190,
            ),
            FakeResponse(
                [
                    text_block(
                        "Built the book library API. Asked the data agent for the schema "
                        "rather than inventing one, built api/books.py against the books "
                        "table they specified, published the contract and told the UI agent."
                    )
                ],
                "end_turn",
                5200,
                300,
            ),
        ],
        "database": [
            FakeResponse(
                [
                    tool_use_block(
                        "append_project_context",
                        {"heading": "Books table schema", "body": SCHEMA},
                        "p2",
                    )
                ],
                "tool_use",
                2400,
                340,
            ),
            FakeResponse([text_block(SCHEMA)], "end_turn", 2700, 210),
        ],
        "frontend": [
            FakeResponse([tool_use_block("list_files", {"path": "."}, "l1")], "tool_use", 2100, 90),
            FakeResponse(
                [
                    text_block(
                        "Noted — the contract is published. Nothing for me to build until "
                        "I'm given a view to make."
                    )
                ],
                "end_turn",
                2300,
                140,
            ),
        ],
    }

    class Scripts:
        def __call__(self, agent_key: str) -> FakeClient:
            script = scripts.get(
                agent_key,
                [FakeResponse([text_block("Nothing for me here yet.")], "end_turn", 1800, 80)],
            )
            return _slow(FakeClient(script), delay)

    return Scripts()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--delay",
        type=float,
        default=1.4,
        help="seconds per model call, so the room animates at a watchable pace",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="dispatch the book-library task on startup",
    )
    args = parser.parse_args()

    import app.models  # noqa: F401
    from app.events.bus import EventBus
    from app.workers import queue as queue_module
    from app.workers.queue import TaskWorkerPool

    engine = dbm.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(dbm.Base.metadata.create_all)
    await dbm.dispose_engine()

    bus = EventBus()
    pool = TaskWorkerPool(
        settings=get_settings(), bus=bus, client_factory=build_scripts(args.delay)
    )
    queue_module._pool = pool

    import app.api.tasks as tasks_api
    import app.api.ws as ws_api

    tasks_api.get_worker_pool = lambda: pool
    tasks_api.get_event_bus = lambda: bus
    ws_api.get_event_bus = lambda: bus

    from app.main import app as fastapi_app

    print(f"\n  demo backend on http://127.0.0.1:{args.port}")
    print(f"  workspace: {WORKDIR / 'workspace'}")
    print("  no API key needed — the model is scripted\n")

    config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    serve = asyncio.create_task(server.serve())

    if args.seed:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        import httpx

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{args.port}") as http:
            await http.post(
                "/api/tasks",
                json={
                    "agent_key": "backend",
                    "description": "Build a REST API for a book library with search",
                },
            )
        print("  seeded: the backend agent is starting the book library task\n")

    await serve


if __name__ == "__main__":
    asyncio.run(main())
