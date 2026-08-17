"""Async database engine wrapper around SQLAlchemy with asyncpg/aiosqlite support."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..core.config import Settings
from ..core.errors import DatabaseError

log = logging.getLogger(__name__)


class Database:
    """Owns the engine and session factory. Created once at startup."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise DatabaseError("Database not initialized")
        return self._engine

    async def connect(self) -> None:
        if self._engine is not None:
            return
        url = self._settings.database_url
        # SQLite needs special flags for cross-thread async access.
        connect_args: dict = {}
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            connect_args["timeout"] = 30  # seconds busy-timeout for concurrent writers
        self._engine = create_async_engine(
            url,
            connect_args=connect_args,
            pool_pre_ping=True,
            future=True,
        )
        if url.startswith("sqlite"):
            # Use WAL journal mode + busy timeout so concurrent async sessions
            # (pooled connections) do not trip "database is locked".
            from sqlalchemy import event

            @event.listens_for(self._engine.sync_engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # noqa: ANN001
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

        self._session_factory = async_sessionmaker(
            bind=self._engine, expire_on_commit=False, class_=AsyncSession
        )
        log.info("database connected url=%s", _redact_url(url))

    async def disconnect(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            log.info("database disconnected")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._session_factory is None:
            raise DatabaseError("Database not initialized")
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def healthcheck(self) -> bool:
        try:
            from sqlalchemy import text

            async with self.session() as s:
                await s.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("database healthcheck failed: %s", exc)
            return False


_singleton: Database | None = None


def get_database() -> Database:
    global _singleton
    if _singleton is None:
        raise DatabaseError("Database not initialized; call init_database() first")
    return _singleton


def init_database(settings: Settings) -> Database:
    global _singleton
    if _singleton is None:
        _singleton = Database(settings)
    return _singleton


def _redact_url(url: str) -> str:
    if "@" not in url:
        return url
    head, tail = url.split("@", 1)
    return f"***@{tail}"
