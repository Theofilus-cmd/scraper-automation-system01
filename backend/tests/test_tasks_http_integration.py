"""Regression coverage for repeated Celery task invocations within the
SAME worker process -- the "got Future <...> attached to a different
loop" / "Event loop is closed" bug (doc 17 hotfix, see
app/workers/async_bridge.py).

Calls `scrape_source_url` -- the actual `@celery_app.task`-decorated
function -- directly, the same way a Celery prefork worker calls it: task
objects remain plain callables, and `.delay()`/`.apply_async()` is only
needed to go through the broker, not to run the task body. This exercises
the exact sync->async bridge a real worker process uses across multiple
tasks without needing a live Celery broker/worker round trip -- see
test_scrapes_integration.py for the separate, full HTTP-endpoint-level
coverage (real broker dispatch, real polling).

Requires real Postgres (upsert_scrape_result(), and by extension the
timezone-aware `scraped_at` fix in app/db/models/base.py) and real
mock-store over HTTP (adapter.fetch()) -- both already required by the
other integration files; run inside the api container's real Compose
network:

    docker compose exec api pytest -m integration
"""

import pytest

from app.workers.tasks_http import scrape_source_url

pytestmark = pytest.mark.integration

_MOCK_STORE_BASE_URL = "http://mock-store:4000"


def test_sequential_task_invocations_reuse_the_worker_loop() -> None:
    """Three calls in a row, in this one process -- exactly what a
    prefork worker child does across its lifetime. Before the hotfix, the
    second call raised 'got Future <...> attached to a different loop'
    because each call's asyncio.run() tore down the loop the shared async
    DB engine's connection pool depended on.
    """
    first = scrape_source_url(f"{_MOCK_STORE_BASE_URL}/products/widget-in-stock")
    second = scrape_source_url(f"{_MOCK_STORE_BASE_URL}/products/widget-out-of-stock")
    third = scrape_source_url(f"{_MOCK_STORE_BASE_URL}/products/widget-in-stock")

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert third["status"] == "completed"

    # third re-scrapes the same identity as first -- proves the DB engine
    # (not just the HTTP fetch) survived across all three calls.
    assert third["created"] is False
    assert third["product"]["id"] == first["product"]["id"]


def test_sequential_calls_survive_a_failure_in_between() -> None:
    """A failed scrape (invalid write, doc 08 §4) returns early via a
    different code path than success (tasks_http._scrape_source_url) --
    it must not corrupt the worker's loop/engine state for the *next*
    task, so this is not automatically covered by the all-success test
    above.
    """
    ok = scrape_source_url(f"{_MOCK_STORE_BASE_URL}/products/widget-in-stock")
    failed = scrape_source_url(f"{_MOCK_STORE_BASE_URL}/products/widget-missing-price")
    ok_again = scrape_source_url(f"{_MOCK_STORE_BASE_URL}/products/widget-out-of-stock")

    assert ok["status"] == "completed"
    assert failed["status"] == "failed"
    assert failed["error_reason"] == "missing_required_field"
    assert ok_again["status"] == "completed"
