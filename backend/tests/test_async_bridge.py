"""Fast, DB-less regression tests for app/workers/async_bridge.py (doc 17
hotfix) -- pure asyncio-level coverage of the loop-reuse mechanism itself,
independent of the slower, real-Postgres/real-mock-store coverage in
tests/test_tasks_http_integration.py.
"""

import asyncio

from app.workers import async_bridge


def test_run_async_reuses_the_same_loop_across_calls() -> None:
    """The original bridge (`asyncio.run()` per call) creates and
    destroys a new loop every time, which is what broke the shared async
    DB engine's connection pool across sequential Celery tasks (see
    async_bridge.py's module docstring). `run_async()` must hand back the
    SAME loop object on every call within one process.
    """
    seen_loop_ids: list[int] = []

    async def _record_running_loop_id() -> None:
        seen_loop_ids.append(id(asyncio.get_running_loop()))

    try:
        async_bridge.run_async(_record_running_loop_id())
        async_bridge.run_async(_record_running_loop_id())
        async_bridge.run_async(_record_running_loop_id())

        assert len(seen_loop_ids) == 3
        assert seen_loop_ids[0] == seen_loop_ids[1] == seen_loop_ids[2]
    finally:
        # Test hygiene only -- production code relies on
        # worker_process_shutdown for this, which never fires in a plain
        # pytest run (it's a Celery-specific signal).
        async_bridge._reset_loop_for_tests()


def test_run_async_recovers_after_the_loop_was_closed() -> None:
    """If the loop was already closed (e.g. after a prior shutdown), the
    next call must transparently create a new one rather than raising
    RuntimeError('Event loop is closed') -- the exact error this hotfix
    exists to eliminate from the normal multi-task path.
    """

    async def _noop() -> str:
        return "ok"

    try:
        assert async_bridge.run_async(_noop()) == "ok"
        async_bridge._get_loop().close()
        assert async_bridge.run_async(_noop()) == "ok"
    finally:
        async_bridge._reset_loop_for_tests()


def test_run_async_propagates_exceptions_without_corrupting_the_loop() -> None:
    """A task that raises must not leave the loop in a state where the
    *next* call fails too -- mirrors tasks_http.py's failure path (a
    failed scrape returns early, on a different branch than success).
    """

    async def _boom() -> None:
        raise ValueError("simulated task failure")

    async def _noop() -> str:
        return "still fine"

    try:
        try:
            async_bridge.run_async(_boom())
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError to propagate")

        assert async_bridge.run_async(_noop()) == "still fine"
    finally:
        async_bridge._reset_loop_for_tests()
