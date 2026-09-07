"""Database engine and session management.

SQLite defaults are wrong for this workload, so three pragmas are applied on
every connection:

  journal_mode=WAL  — concurrent agents writing trace rows will otherwise hit
                      "database is locked"; WAL lets readers and one writer
                      proceed together.
  foreign_keys=ON   — SQLite ships with FK enforcement OFF, which would silently
                      void the relational integrity between tasks, messages,
                      tool calls and usage rows.
  busy_timeout      — wait rather than fail immediately on writer contention.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

BUSY_TIMEOUT_MS = 5_000


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def get_engine():
    """Lazily create the process-wide async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.db_file.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(settings.database_url, echo=False, future=True)
        event.listen(_engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session: commits on success, rolls back on error."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


async def check_db() -> tuple[bool, str]:
    """Health probe. Returns (ok, detail)."""
    try:
        async with get_session_factory()() as session:
            result = await session.execute(text("SELECT 1"))
            result.scalar_one()
            mode = (await session.execute(text("PRAGMA journal_mode"))).scalar_one()
        return True, f"connected (journal_mode={mode})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def dispose_engine() -> None:
    """Close pooled connections. Called on app shutdown and in test teardown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
