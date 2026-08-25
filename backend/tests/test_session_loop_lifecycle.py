"""Regression tests for app/db/session.py's per-event-loop engine cache
(doc 17 hotfix): a single process-lifetime engine, reused unconditionally
across whichever loop happens to be running, is what produced "got
Future <...> attached to a different loop" / "Event loop is closed" once
more than one event loop touched the database in the same process --
exactly what pytest-asyncio's default function-scoped loop does across
back-to-back async tests.

`create_async_engine()` is lazy (it does not connect until first actual
query), so these tests verify the caching/keying logic itself without
needing a live Postgres -- they run as part of the normal, non-integration
`pytest` sweep, not just `-m integration`. The end-to-end proof against a
real database (this fix's actual effect on real cross-test runs) lives in
the `-m integration` suite: test_persistence_integration.py,
test_scrapes_integration.py, and test_tasks_http_integration.py, several
of which now run back-to-back real async DB calls on separate
pytest-asyncio loops -- exactly the scenario that failed before this fix.
"""

import asyncio

from app.db.session import (
    dispose_engine_for_current_loop,
    get_engine,
    get_session_factory,
)


async def test_get_engine_returns_the_same_instance_within_one_loop() -> None:
    first = get_engine()
    second = get_engine()
    assert first is second


def test_get_engine_returns_different_instances_across_different_loops() -> None:
    """The core regression: two separate event loops (pytest-asyncio's
    per-test default) must NOT be handed the same engine object -- that
    would be exactly the cross-loop connection reuse that raised "attached
    to a different loop" in the real integration run.
    """

    async def _get_engine_id() -> int:
        return id(get_engine())

    loop_a = asyncio.new_event_loop()
    try:
        engine_id_a = loop_a.run_until_complete(_get_engine_id())
        loop_a.run_until_complete(dispose_engine_for_current_loop())
    finally:
        loop_a.close()

    loop_b = asyncio.new_event_loop()
    try:
        engine_id_b = loop_b.run_until_complete(_get_engine_id())
        loop_b.run_until_complete(dispose_engine_for_current_loop())
    finally:
        loop_b.close()

    assert engine_id_a != engine_id_b


async def test_dispose_engine_for_current_loop_is_a_safe_no_op_when_nothing_cached() -> None:
    """A test whose loop never touched the database (no prior get_engine()
    call on THIS loop) must not raise when the autouse teardown fixture
    runs -- conftest.py's _dispose_db_engine_after_test relies on this for
    every non-DB test in the suite.
    """
    await dispose_engine_for_current_loop()  # must not raise


async def test_dispose_engine_for_current_loop_removes_the_cache_entry() -> None:
    engine_before = get_engine()
    await dispose_engine_for_current_loop()
    engine_after = get_engine()

    # A fresh engine was created post-dispose, on the same loop -- proves
    # the cache entry was actually dropped, not just the pool emptied.
    assert engine_before is not engine_after


async def test_get_session_factory_is_cached_like_get_engine() -> None:
    first = get_session_factory()
    second = get_session_factory()
    assert first is second
