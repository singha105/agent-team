"""Shared fixtures.

Every test runs against a temporary workspace and a temporary SQLite file, so
nothing here touches the real data/ or workspace/ directories.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from app.core import config as config_module
from app.core import db as db_module


def _reset_caches() -> None:
    config_module.get_settings.cache_clear()
    db_module._engine = None
    db_module._session_factory = None


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[config_module.Settings]:
    """Isolated settings pointing at a temp workspace and DB."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "data").mkdir()

    monkeypatch.setenv("AGENTTEAM_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("AGENTTEAM_DB_PATH", str(tmp_path / "data" / "test.db"))
    monkeypatch.setenv("AGENTTEAM_AGENT_CONFIG_DIR", str(tmp_path / "agents"))
    # Default to subprocess mode so unit tests need no Docker daemon; the
    # integration tests override this explicitly.
    monkeypatch.setenv("AGENTTEAM_SANDBOX_MODE", "subprocess")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    _reset_caches()
    yield config_module.get_settings()
    _reset_caches()


@pytest.fixture
def workspace(settings: config_module.Settings) -> Path:
    return settings.workspace_dir


@pytest.fixture
def agent_config_dir(settings: config_module.Settings) -> Path:
    directory = settings.agent_configs
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest_asyncio.fixture
async def session(settings: config_module.Settings) -> AsyncIterator:
    """A session against a fresh schema created directly from the models."""
    import app.models  # noqa: F401  registers tables

    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_module.Base.metadata.create_all)

    factory = db_module.get_session_factory()
    async with factory() as s:
        yield s
        await s.rollback()

    await db_module.dispose_engine()


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=15, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


requires_docker = pytest.mark.skipif(not docker_available(), reason="needs a running Docker daemon")


# --------------------------------------------------------------------------
# Phase 2: API, worker and event-stream fixtures
# --------------------------------------------------------------------------

REAL_AGENT_CONFIGS = Path(__file__).resolve().parents[2] / "config" / "agents"


@pytest.fixture
def api_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Settings that point at the *real* agent configs.

    The API tests are more useful exercising the four agents that actually
    ship than a synthetic roster — a typo in devops.yaml should fail a test.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "data").mkdir()

    monkeypatch.setenv("AGENTTEAM_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("AGENTTEAM_DB_PATH", str(tmp_path / "data" / "api.db"))
    monkeypatch.setenv("AGENTTEAM_AGENT_CONFIG_DIR", str(REAL_AGENT_CONFIGS))
    monkeypatch.setenv("AGENTTEAM_SANDBOX_MODE", "subprocess")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    _reset_caches()
    from app.agents import config_loader

    config_loader._cached.cache_clear()
    yield config_module.get_settings()
    config_loader._cached.cache_clear()
    _reset_caches()
