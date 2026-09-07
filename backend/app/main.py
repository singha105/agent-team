"""FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.agents.config_loader import AgentConfigError, load_all_agent_configs
from app.api import agents, health, tasks, ws
from app.core.db import dispose_engine
from app.core.logging import get_logger
from app.workers.queue import get_worker_pool

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log.info("AgentTeam %s starting", __version__)

    try:
        keys = sorted(load_all_agent_configs())
    except AgentConfigError as exc:
        # Better to start with no lanes and a loud log than to crash the API:
        # /health still answers, which is how an operator finds out why.
        log.error("could not load agent configs: %s", exc)
        keys = []

    pool = get_worker_pool()
    await pool.start(keys)
    try:
        yield
    finally:
        await pool.stop()
        await dispose_engine()
        log.info("AgentTeam stopped")


app = FastAPI(
    title="AgentTeam",
    description="A simulated software engineering team of AI agents.",
    version=__version__,
    lifespan=lifespan,
)

# The Vite dev server runs on a different origin in development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(agents.router)
app.include_router(tasks.router)
app.include_router(ws.router)
