"""Regression coverage for repeated Celery task invocations within the
SAME worker process -- the "got Future <...> attached to a different
loop" / "Event loop is closed" bug (doc 17 hotfix, see
app/workers/async_bridge.py).

Calls `scrape_source_url` -- the actual `@celery_app.task`-decorated,
`bind=True` function -- directly, the same way a Celery prefork worker
calls it: a bound task remains a plain callable outside a real worker too
(Celery injects a default, empty `self.request` context), and
`.delay()`/`.apply_async()` is only needed to go through the broker, not
to run the task body. This exercises the exact sync->async bridge a real
worker process uses across multiple tasks without needing a live Celery
broker/worker round trip -- see test_scrapes_integration.py for the
separate, full HTTP-endpoint-level coverage (real broker dispatch, real
polling).

Phase 2 signature change (flagged in tasks_http.py's own module docstring):
`scrape_source_url` now takes a `tasks.id` instead of a raw source_url, so
every test here must first create a real source+run+task (durable rows a
real trigger path would also create before ever dispatching, doc 18 §4.1)
before calling it. Test functions here stay plain `def` (NOT `async def`)
deliberately: `run_async()` (app/workers/async_bridge.py) drives its own
persistent event loop via `loop.run_until_complete()`, which raises if
called from a thread where an *different* loop (e.g. pytest-asyncio's, for
an `async def test_*`) is already running -- so the factory setup calls
below are routed through `run_async()` too, keeping every call in a given
test on the exact same persistent loop `scrape_source_url` itself uses,
exactly like a real worker process would.

Requires real Postgres and real mock-store over HTTP; run inside the api
container's real Compose network:

    docker compose exec api pytest -m integration
"""

from typing import Any

import pytest

from app.workers.async_bridge import run_async
from app.workers.tasks_http import scrape_source_url
from tests.factories import (
    MOCK_STORE_BASE_URL,
    create_test_run,
    create_test_source,
    create_test_task,
)

pytestmark = pytest.mark.integration


def _run_scrape_for_url(source_url: str) -> dict[str, Any]:
    """Creates a fresh source+run+task for `source_url` (or reuses the
    source if this exact URL already has a non-archived row -- doc 18
    §6.1's idempotency, same as production) and runs the task synchronously,
    all on the worker-bridge's persistent loop.
    """

    async def _setup() -> str:
        source = await create_test_source(url=source_url)
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)
        return str(task.id)

    task_id = run_async(_setup())
    return scrape_source_url(task_id)


def test_sequential_task_invocations_reuse_the_worker_loop() -> None:
    """Three calls in a row, in this one process -- exactly what a
    prefork worker child does across its lifetime. Before the doc 17
    hotfix, the second call raised 'got Future <...> attached to a
    different loop' because each call's own asyncio.run() tore down the
    loop the shared async DB engine's connection pool depended on.
    """
    first = _run_scrape_for_url(f"{MOCK_STORE_BASE_URL}/products/widget-in-stock")
    second = _run_scrape_for_url(f"{MOCK_STORE_BASE_URL}/products/widget-out-of-stock")
    third = _run_scrape_for_url(f"{MOCK_STORE_BASE_URL}/products/widget-in-stock")

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert third["status"] == "completed"

    # third re-scrapes the same identity as first, two calls later on the
    # same persistent loop -- proves the DB engine (not just the HTTP
    # fetch) survived across all three calls, regardless of whether this
    # exact identity had ever been seen by an earlier test in the suite.
    assert third["created"] is False
    assert third["product_id"] == first["product_id"]


def test_sequential_calls_survive_a_failure_in_between() -> None:
    """A failed scrape (invalid write, doc 08 §4) returns early via a
    different code path than success -- it must not corrupt the worker's
    loop/engine state for the *next* task, so this is not automatically
    covered by the all-success test above.
    """
    ok = _run_scrape_for_url(f"{MOCK_STORE_BASE_URL}/products/widget-in-stock")
    failed = _run_scrape_for_url(f"{MOCK_STORE_BASE_URL}/products/widget-missing-price")
    ok_again = _run_scrape_for_url(f"{MOCK_STORE_BASE_URL}/products/widget-out-of-stock")

    assert ok["status"] == "completed"
    assert failed["status"] == "failed"
    assert failed["error_reason"] == "missing_required_field"
    assert ok_again["status"] == "completed"
