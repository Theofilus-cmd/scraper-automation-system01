"""End-to-end regression test for the cross-event-loop DB engine bug
(doc 17 hotfix #2): pytest-asyncio gives each `async def test_*` its own,
separate event loop by default. Before the loop-keyed engine cache in
app/db/session.py, the SECOND async test to touch the database in any
given pytest run inherited the FIRST test's engine -- bound to a loop
that had already closed by the time the second test ran -- and crashed
with "got Future <...> attached to a different loop" /
"RuntimeError: Event loop is closed". This was not hypothetical:
test_persistence_integration.py alone already has four async test
functions in one pytest session, and reproduced this exact failure on
the second one to run, in real Docker verification.

This file makes the "back-to-back, on separate pytest-asyncio loops"
scenario explicit and self-contained -- two clearly separate test
functions, each performing a real, independent database write -- rather
than relying on a reader to notice that the fix for it is implicit in
every other multi-test integration file in this suite.

Requires real Postgres and real mock-store:

    docker compose exec api pytest -m integration
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.db.models.repository import upsert_scrape_result
from app.scraping.types import NormalizedRecord, ValidationResult

pytestmark = pytest.mark.integration


def _record(source_url: str) -> NormalizedRecord:
    return NormalizedRecord(
        product_url=source_url,
        scraped_at=datetime.now(UTC),
        stock_status="in_stock",
        product_name="Cross-Loop Regression Widget",
        price=Decimal("9.99"),
        currency="USD",
        sku=f"CROSS-LOOP-{uuid.uuid4().hex}",
    )


async def test_first_async_test_writes_successfully() -> None:
    """Runs on pytest-asyncio's event loop #1 for this pytest session."""
    source_url = f"http://mock-store:4000/products/cross-loop-a-{uuid.uuid4().hex}"
    result = await upsert_scrape_result(
        source_url=source_url,
        adapter_slug="mock_store",
        record=_record(source_url),
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
    source_url = f"http://mock-store:4000/products/cross-loop-b-{uuid.uuid4().hex}"
    result = await upsert_scrape_result(
        source_url=source_url,
        adapter_slug="mock_store",
        record=_record(source_url),
        validation=ValidationResult(is_valid=True, errors={}),
    )
    assert result.is_valid is True
