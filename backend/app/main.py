"""FastAPI application entry point.

Phase 1 exposes only /health. Task and WebSocket routers arrive in later phases.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import health
from app.core.db import dispose_engine
from app.core.logging import get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log.info("AgentTeam %s starting", __version__)
    yield
    await dispose_engine()
    log.info("AgentTeam stopped")


app = FastAPI(
    title="AgentTeam",
    description="A simulated software engineering team of AI agents.",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(health.router)
