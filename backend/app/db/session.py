"""Async database engine and session factory.

Phase 0 has no ORM models yet -- this module only establishes the
connection machinery that later phases will build on, plus a helper used
by the readiness check and by future migration/bootstrap code.

Connections route through PgBouncer in transaction-pooling mode (see docs
04/05), so asyncpg's client-side prepared-statement cache is disabled via
statement_cache_size=0. Without this, PgBouncer can hand a pooled
connection that prepared a statement to a different logical session,
producing "prepared statement does not exist" errors.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            connect_args={"statement_cache_size": 0},
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def check_database_connection() -> bool:
    """Run a trivial query to confirm the database is reachable."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("database connection check failed")
        return False
