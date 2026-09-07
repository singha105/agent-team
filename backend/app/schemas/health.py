"""Response schemas for the health endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DatabaseHealth(BaseModel):
    ok: bool = Field(description="Whether a query against the database succeeded.")
    detail: str = Field(description="Connection detail, or the error if the probe failed.")


class HealthResponse(BaseModel):
    status: str = Field(description="'ok' if every dependency is healthy, else 'degraded'.")
    version: str = Field(description="AgentTeam version.")
    database: DatabaseHealth
    sandbox_mode: str = Field(description="Configured command execution mode.")
    api_key_configured: bool = Field(
        description="Whether ANTHROPIC_API_KEY is set. The key itself is never returned."
    )
