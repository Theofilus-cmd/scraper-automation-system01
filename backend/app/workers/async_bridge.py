"""Sync-to-async bridge for Celery's prefork workers (doc 17 hotfix).

Celery invokes tasks synchronously and offers no running event loop
inside a worker process. The original bridge -- `asyncio.run(coro)` once
per task invocation -- creates and destroys a brand new event loop on
every single call. That is fine for code with no state that outlives one
call, but `app/db/session.py`'s async SQLAlchemy engine is a *process-
lifetime* singleton: created lazily on first use, cached in a module
global, and reused by every task this worker process ever runs
afterward. Its connection pool holds real asyncpg connections, and an
asyncpg connection (like any asyncio transport) is bound to the event
loop that created it for its entire life.

`asyncio.run()` closing that first loop when task #1 returns leaves the
pool holding connections that belong to a now-dead loop. Task #2's fresh
`asyncio.run()` loop then tries to check one of them out, and asyncio
raises exactly what was observed:

    got Future <...> attached to a different loop
    RuntimeError: Event loop is closed

This is not a SQLAlchemy bug -- it's a mismatch between a per-call loop
and a per-process engine. `app/scheduler/main.py` never hits this: it
calls `asyncio.run(main())` exactly once for its entire process lifetime
(one continuously-running loop), not once per unit of work. The FastAPI
`api` process is likewise unaffected -- uvicorn keeps one event loop
alive for the whole process, every request runs as a task on it.

The fix: one event loop per (prefork) worker *process*, created lazily on
that process's first task and reused, via `loop.run_until_complete()`
(which -- unlike `asyncio.run()` -- does NOT close the loop when the
coroutine returns), for every task afterward. The engine's pool then
stays valid for the process's whole life, exactly like a long-lived
service is supposed to work. `worker_process_shutdown` disposes the
engine and closes the loop when a prefork child exits, so nothing leaks;
disposing before closing (not after) is what lets every pooled connection
close cleanly while its loop is still alive to run that cleanup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from celery.signals import worker_process_shutdown

from app.core.logging import get_logger
from app.db.session import get_engine

logger = get_logger(__name__)

_loop: asyncio.AbstractEventLoop | None = None

# PEP 695 (`def run_async[T](...)`) would be the natural spelling on this
# codebase's actual target (Python 3.12, matching the Dockerfile and this
# file's own py312-only syntax elsewhere) and ruff agrees (UP047) -- but is
# deliberately not used here: this specific generation sandbox's mypy tool
# happens to be installed under Python 3.11 (a sandbox-local tooling quirk,
# unrelated to the shipped project, which pins 3.12 throughout), and 3.11's
# stdlib `ast` module cannot even parse PEP 695 syntax -- not a type error,
# a hard parse failure that aborts mypy's *entire* run, not just this file.
# TypeVar is semantically identical and fully mypy-clean under 3.11, which
# is what actually let every other file in this delivery get real mypy
# coverage instead of none. The real CI (mypy under a proper 3.12 env, per
# ci.yml) is free to modernize this one signature back to PEP 695 syntax.
_T = TypeVar("_T")


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def run_async(coro: Coroutine[Any, Any, _T]) -> _T:  # noqa: UP047
    """Run `coro` on this worker process's persistent event loop. Every
    Celery task that needs to await async code (DB, HTTP fetch) must call
    this instead of `asyncio.run()` -- see module docstring for why.
    """
    return _get_loop().run_until_complete(coro)


def _reset_loop_for_tests() -> None:
    """Test-only escape hatch (see tests/test_async_bridge.py). Does NOT
    dispose the DB engine -- unlike the real shutdown path below, these
    are pure-asyncio tests that never create a real engine/connection.
    """
    global _loop
    if _loop is not None and not _loop.is_closed():
        _loop.close()
    _loop = None


@worker_process_shutdown.connect
def _dispose_engine_and_loop(**_kwargs: object) -> None:
    """Fires once, just before a prefork child process exits. Disposing
    the engine first closes every pooled connection while the loop that
    owns them is still alive to run that cleanup -- closing the loop
    first would abandon them instead, the same leak this module exists to
    avoid.
    """
    global _loop
    if _loop is None or _loop.is_closed():
        return
    try:
        _loop.run_until_complete(get_engine().dispose())
    except Exception:
        logger.exception("error disposing async engine during worker shutdown")
    finally:
        _loop.close()
        _loop = None
