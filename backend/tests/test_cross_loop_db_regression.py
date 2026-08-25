"""End-to-end regression test for the cross-event-loop DB engine bug
(doc 17 hotfix #2): pytest-asyncio gives each `async def test_*` its own,
separate event loop by default. Before the loop-keyed engine cache in
app/db/session.py, the SECOND async test to touch the database in any
given pytest run inherited the FIRST test's engine -- bound to a loop
that had already closed by the time the second test ran -- and crashed
with "got Future <...> attached to a different loop" /
"RuntimeError: Event loop is closed". This was not hypothetical:
test_persistence_integration.py alone already has several async test
functions in one pytest session, and reproduced this exact failure on
the second one to run, in real Docker verification.

This file makes the "back-to-back, on separate pytest-asyncio loops"
scenario explicit and self-contained -- two clearly separate test
functions, each performing a real, independent database write -- rather
than relying on a reader to notice that the fix for it is implicit in
every other multi-test integration file in this suite.

Acceptance-review fix: this file predates Phase 2 and was never migrated
off Phase 1's `upsert_scrape_result(source_url=, adapter_slug=, ...)`
contract when `repository.py`'s signature changed for Phase 2 (see that
module's own docstring for why) -- left calling a keyword argument set
that no longer exists:

    TypeError: upsert_scrape_result() got an unexpected keyword
    argument 'source_url'

Fixed by giving each test a real, production-realistic source+run+task
via tests/factories.py first (the same order app/workers/tasks_http.py
itself always follows -- doc 18 §4.1's phases A/B happen before a task
ever runs), then calling upsert_scrape_result() with its current
source_id/run_id/task_id contract. The actual regression this file exists
to prove -- that a second async test on a second, freshly opened
pytest-asyncio loop can still reach the database -- is entirely unaffected
by that signature change and still holds here: each test below still
performs one real, independent write on its own loop, each against its
own fresh, uuid4-suffixed source (create_source_run_task() ->
create_test_source() -> factories.unique_source_url()), so there is no
risk of the two tests colliding on any constraint, including
uq_runs_one_in_flight_per_source.

Requires real Postgres and real mock-store:

    docker compose exec api pytest -m integration
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.db.models.repository import upsert_scrape_result
from app.scraping.types import NormalizedRecord, ValidationResult
from tests.factories import create_source_run_task

pytestmark = pytest.mark.integration


def _record(source_url: str, *, sku_prefix: str) -> NormalizedRecord:
    return NormalizedRecord(
        product_url=source_url,
        scraped_at=datetime.now(UTC),
        stock_status="in_stock",
        product_name="Cross-Loop Regression Widget",
        price=Decimal("9.99"),
        currency="USD",
        sku=f"{sku_prefix}-{uuid.uuid4().hex}",
    )


async def test_first_async_test_writes_successfully() -> None:
    """Runs on pytest-asyncio's event loop #1 for this pytest session."""
    source, run, task = await create_source_run_task()

    result = await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=_record(source.url, sku_prefix="CROSS-LOOP-A"),
        validation=ValidationResult(is_valid=True, errors={}),
    )

    assert result.is_valid is True


async def test_second_async_test_also_writes_successfully() -> None:
    """Runs on a DIFFERENT event loop than the test above (pytest-asyncio's
    default function-scoped loop -- a fresh one per test function, closed
    at the end of the previous test). Before the loop-keyed engine cache
    fix, this raised "got Future <...> attached to a different loop"
    because it inherited the previous test's cached engine, whose pooled
    connections were bound to that now-closed loop.
    """
    source, run, task = await create_source_run_task()

    result = await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=_record(source.url, sku_prefix="CROSS-LOOP-B"),
        validation=ValidationResult(is_valid=True, errors={}),
    )

    assert result.is_valid is True
