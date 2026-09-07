"""The /health endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import app.models  # noqa: F401  registers tables
from app import __version__
from app.core import db as db_module
from app.main import app as fastapi_app


@pytest.fixture
async def client(settings):
    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_module.Base.metadata.create_all)

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db_module.dispose_engine()


async def test_health_reports_version_and_db(client) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["database"]["ok"] is True
    assert "wal" in body["database"]["detail"].lower()


async def test_health_never_leaks_the_api_key(client, settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-secret-value")
    response = await client.get("/health")
    assert response.json()["api_key_configured"] is True
    assert "sk-ant-secret-value" not in response.text


async def test_health_returns_503_when_the_db_is_unreachable(client, monkeypatch) -> None:
    async def broken():
        return False, "OperationalError: unable to open database file"

    monkeypatch.setattr("app.api.health.check_db", broken)
    response = await client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
