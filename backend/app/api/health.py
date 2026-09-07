"""Health endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app import __version__
from app.core.config import get_settings
from app.core.db import check_db
from app.schemas.health import DatabaseHealth, HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    """Report version and dependency status.

    Returns 503 when the database is unreachable so that a container
    orchestrator or CI check fails on a broken deployment rather than
    reading a 200 with an 'unhealthy' body.
    """
    settings = get_settings()
    db_ok, db_detail = await check_db()

    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=__version__,
        database=DatabaseHealth(ok=db_ok, detail=db_detail),
        sandbox_mode=settings.sandbox_mode,
        # Reports presence only — the key value never leaves the process.
        api_key_configured=bool(settings.anthropic_api_key),
    )
