"""Async database engine and session factory.

Connections route through PgBouncer in transaction-pooling mode (see docs
04/05), so asyncpg's client-side prepared-statement cache is disabled via
statement_cache_size=0. Without this, PgBouncer can hand a pooled
connection that prepared a statement to a different logical session,
producing "prepared statement does not exist" errors.

Engine lifecycle (doc 17 hotfix): an AsyncEngine's connection pool holds
real asyncpg connections, and each one is bound, for its entire life, to
whichever event loop was running when it was first checked out -- reusing
it from a *different* loop raises "got Future <...> attached to a
different loop" or "RuntimeError: Event loop is closed", not a
SQLAlchemy bug, a fundamental asyncio constraint. A single process-
lifetime engine object (the previous design here: one `_engine` global,
created lazily on first use) is safe ONLY as long as exactly one event
loop ever uses it for that process's whole life. That is true for the
FastAPI `api` process (uvicorn keeps one loop alive for the whole
process) and true for a Celery prefork worker process
(app/workers/async_bridge.py gives each one persistent, reused loop) --
but it is NOT true inside a pytest-asyncio run, where (by default) every
`async def test_*` gets its own fresh, function-scoped event loop, closed
at the end of that test. The very first async integration test to touch
the database created and cached an engine bound to *that* test's loop;
the second async integration test -- a different loop -- then inherited
a "shared" engine whose pool held connections tied to the first, already-
closed loop.

The general fix: key the cached engine (and its session factory) by the
identity of the CURRENTLY RUNNING event loop, not by process. The first
async DB call on a given loop creates and caches an engine bound to that
loop; every later call *on that same loop* reuses it (real pooling still
works within one loop's lifetime -- one FastAPI process, one Celery
worker process, or one pytest test); a call from a *different* loop
transparently gets its own, separate engine instead of unsafely reusing
one bound to a loop it doesn't own. This one change is what makes the
lifecycle correct in all three contexts at once, without special-casing
any of them.

A `WeakKeyDictionary` (not a plain dict keyed by `id(loop)`) is used
deliberately: `id()` is a memory address in CPython, and a garbage-
collected loop's address CAN be reused by a later, unrelated loop object
-- an `id()`-keyed cache could then hand back a stale engine for a
completely different loop. Keying by the loop object itself avoids that
hazard, and lets an entry for a loop nothing references anymore be
dropped automatically. That is a safety net, not the primary cleanup
mechanism, though: garbage collection timing is not a substitute for
explicit disposal, which is why `dispose_engine_for_current_loop()`
exists below and is called explicitly -- from tests/conftest.py's
autouse per-test fixture (before pytest-asyncio closes that test's loop)
and from app/workers/async_bridge.py's `worker_process_shutdown` handler
(before a prefork child's loop closes) -- so every pooled connection is
closed *while its own loop is still alive to run that cleanup*, with
nothing leaked and nothing left dangling for whatever runs next to
accidentally inherit.
"""

import asyncio
import weakref
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

_engines: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncEngine]" = (
    weakref.WeakKeyDictionary()
)
_session_factories: (
    "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, async_sessionmaker[AsyncSession]]"
) = weakref.WeakKeyDictionary()


def get_engine() -> AsyncEngine:
    """Returns the AsyncEngine bound to the CURRENTLY RUNNING event loop,
    creating one on first use per loop. Must be called from inside a
    running loop (every real call site is `async def` or awaited from
    one) -- see module docstring for why this is keyed by loop at all.
    """
    loop = asyncio.get_running_loop()
    engine = _engines.get(loop)
    if engine is None:
        engine = create_async_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            connect_args={"statement_cache_size": 0},
        )
        _engines[loop] = engine
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    loop = asyncio.get_running_loop()
    factory = _session_factories.get(loop)
    if factory is None:
        factory = async_sessionmaker(get_engine(), expire_on_commit=False)
        _session_factories[loop] = factory
    return factory


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


async def dispose_engine_for_current_loop() -> None:
    """Disposes and forgets the CURRENT loop's cached engine, if any --
    closing every pooled connection while this loop is still alive to run
    that cleanup, then dropping the cache entry. Safe to call even when
    no engine was ever created on this loop (a no-op). Must be called
    BEFORE the current loop closes, never after -- see module docstring.
    """
    loop = asyncio.get_running_loop()
    engine = _engines.pop(loop, None)
    _session_factories.pop(loop, None)
    if engine is not None:
        await engine.dispose()
