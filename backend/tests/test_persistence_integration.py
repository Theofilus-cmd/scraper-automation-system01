"""Direct upsert_scrape_result() tests -- doc 08 §4's write-gating rule
plus doc 18 §3.3's observation-history diff-and-append, against a real
Postgres:

    docker compose exec api pytest -m integration

Deliberately bypasses HTTP/Celery entirely (unlike test_scrapes_integration.
py) so it can exercise cases that genuinely need the SAME product identity
scraped twice with different outcomes -- not reachable through the
mock-store HTTP fixtures alone, since each of mock-store's 4 slugs is a
distinct product identity.

Phase 2 signature change (flagged in repository.py's own module docstring):
upsert_scrape_result() now takes an already-resolved source_id plus a
run_id/task_id (provenance for observation_history), instead of a raw
source_url/adapter_slug -- so every test here first creates a real
source+run+task via tests/factories.py before calling it, the same order
app/workers/tasks_http.py itself always does.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models.lifecycle import ObservationHistory
from app.db.models.repository import upsert_scrape_result
from app.db.models.scraping import CurrentObservation, Product, Source
from app.db.session import get_session
from app.scraping.types import NormalizedRecord, ValidationResult
from tests.factories import create_source_run_task, create_test_run, create_test_task

pytestmark = pytest.mark.integration


def _valid_record(
    source_url: str,
    *,
    price: str = "19.99",
    sku: str = "TEST-SKU-1",
    stock_status: str = "in_stock",
) -> NormalizedRecord:
    return NormalizedRecord(
        product_url=source_url,
        scraped_at=datetime.now(UTC),
        stock_status=stock_status,
        product_name="Integration Test Widget",
        price=Decimal(price),
        currency="USD",
        sku=sku,
    )


def _invalid_record(source_url: str, *, sku: str = "TEST-SKU-1") -> NormalizedRecord:
    return NormalizedRecord(
        product_url=source_url,
        scraped_at=datetime.now(UTC),
        stock_status="in_stock",
        product_name="Integration Test Widget",
        price=None,  # missing required field -- doc 08 §4 failure
        currency="USD",
        sku=sku,
    )


async def _fetch_observation(product_id: uuid.UUID) -> CurrentObservation | None:
    async with get_session() as session:
        result = await session.execute(
            select(CurrentObservation).where(CurrentObservation.product_id == product_id)
        )
        return result.scalar_one_or_none()


async def _count_products_for_source(source_id: uuid.UUID) -> int:
    async with get_session() as session:
        result = await session.execute(select(Product).where(Product.source_id == source_id))
        return len(result.scalars().all())


async def _history_rows_for_product(product_id: uuid.UUID) -> list[ObservationHistory]:
    async with get_session() as session:
        result = await session.execute(
            select(ObservationHistory)
            .where(ObservationHistory.product_id == product_id)
            .order_by(ObservationHistory.version_created_at.asc())
        )
        return list(result.scalars().all())


async def _new_run_task_for_source(source_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """A second (or third...) scrape *event* for an already-existing
    source is, in the real system, always a new run with its own new task
    -- never a second call against the same task_id (doc 18 §2.3's
    cardinality note: a task is retried, a run is not re-created). Mirrors
    that here rather than reusing the first call's ids.
    """
    run = await create_test_run(source_id)
    task = await create_test_task(run.id, source_id)
    return run.id, task.id


async def test_first_sighting_valid_creates_product_observation_and_history() -> None:
    source, run, task = await create_source_run_task()
    record = _valid_record(source.url)

    result = await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=record,
        validation=ValidationResult(is_valid=True, errors={}),
    )

    assert result.is_valid is True
    assert result.created is True
    assert result.history_appended is True
    assert result.product_id is not None

    observation = await _fetch_observation(result.product_id)
    assert observation is not None
    assert observation.price == Decimal("19.99")
    assert observation.is_valid is True
    # doc 17 hotfix regression: a first-ever valid write's scraped_at must
    # round-trip through Postgres still timezone-aware.
    assert observation.scraped_at.tzinfo is not None

    history = await _history_rows_for_product(result.product_id)
    assert len(history) == 1
    assert history[0].change_summary is None  # doc 18 §3.3: null on first sighting
    assert history[0].run_id == run.id
    assert history[0].task_id == task.id


async def test_repeat_valid_scrape_updates_in_place_not_duplicated() -> None:
    source, run, task = await create_source_run_task()

    first = await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=_valid_record(source.url, price="19.99"),
        validation=ValidationResult(is_valid=True, errors={}),
    )
    run2_id, task2_id = await _new_run_task_for_source(source.id)
    second = await upsert_scrape_result(
        source_id=source.id,
        run_id=run2_id,
        task_id=task2_id,
        record=_valid_record(source.url, price="24.99"),
        validation=ValidationResult(is_valid=True, errors={}),
    )

    assert first.created is True
    assert second.created is False
    assert first.product_id == second.product_id
    assert await _count_products_for_source(source.id) == 1

    observation = await _fetch_observation(second.product_id)
    assert observation is not None
    assert observation.price == Decimal("24.99")


async def test_changed_field_appends_history_row_with_diff_only() -> None:
    """doc 18 §3.3: a re-scrape that changes at least one tracked field
    appends a new history row whose change_summary names only the field(s)
    that actually differ.
    """
    source, run, task = await create_source_run_task()
    await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=_valid_record(source.url, price="19.99", stock_status="in_stock"),
        validation=ValidationResult(is_valid=True, errors={}),
    )
    run2_id, task2_id = await _new_run_task_for_source(source.id)
    second = await upsert_scrape_result(
        source_id=source.id,
        run_id=run2_id,
        task_id=task2_id,
        record=_valid_record(source.url, price="17.49", stock_status="in_stock"),
        validation=ValidationResult(is_valid=True, errors={}),
    )

    assert second.history_appended is True
    assert second.product_id is not None
    history = await _history_rows_for_product(second.product_id)
    assert len(history) == 2
    latest = history[-1]
    assert latest.change_summary is not None
    assert set(latest.change_summary.keys()) == {"price"}
    assert latest.change_summary["price"] == {"from": "19.99", "to": "17.49"}


async def test_unchanged_valid_rescrape_appends_no_history_row() -> None:
    """doc 18 §3.3: identical tracked-field values on a re-scrape update
    current_observations (scraped_at moves forward) but append nothing to
    observation_history.
    """
    source, run, task = await create_source_run_task()
    first = await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=_valid_record(source.url, price="19.99"),
        validation=ValidationResult(is_valid=True, errors={}),
    )
    run2_id, task2_id = await _new_run_task_for_source(source.id)
    second = await upsert_scrape_result(
        source_id=source.id,
        run_id=run2_id,
        task_id=task2_id,
        record=_valid_record(source.url, price="19.99"),
        validation=ValidationResult(is_valid=True, errors={}),
    )

    assert second.history_appended is False
    assert second.product_id == first.product_id
    assert first.product_id is not None
    history = await _history_rows_for_product(first.product_id)
    assert len(history) == 1  # still just the first-sighting row


async def test_invalid_write_after_valid_leaves_prior_snapshot_standing() -> None:
    """The one case that genuinely needs the same identity scraped twice
    with different outcomes -- doc 08 §4's core guarantee, and the reason
    this file exists separately from the mock-store HTTP fixtures.
    """
    source, run, task = await create_source_run_task()

    valid_result = await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=_valid_record(source.url, price="19.99"),
        validation=ValidationResult(is_valid=True, errors={}),
    )
    assert valid_result.product_id is not None

    run2_id, task2_id = await _new_run_task_for_source(source.id)
    invalid_result = await upsert_scrape_result(
        source_id=source.id,
        run_id=run2_id,
        task_id=task2_id,
        record=_invalid_record(source.url),
        validation=ValidationResult(
            is_valid=False, errors={"price": "missing required field: price"}
        ),
    )

    assert invalid_result.is_valid is False
    assert invalid_result.product_id is None
    assert invalid_result.history_appended is False

    # The prior valid snapshot must be untouched.
    observation = await _fetch_observation(valid_result.product_id)
    assert observation is not None
    assert observation.price == Decimal("19.99")
    assert observation.is_valid is True

    assert await _count_products_for_source(source.id) == 1
    history = await _history_rows_for_product(valid_result.product_id)
    assert len(history) == 1  # the invalid write appended nothing


async def test_first_sighting_invalid_creates_no_product_or_observation() -> None:
    source, run, task = await create_source_run_task()

    result = await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=_invalid_record(source.url),
        validation=ValidationResult(
            is_valid=False, errors={"price": "missing required field: price"}
        ),
    )

    assert result.is_valid is False
    assert result.product_id is None
    assert result.history_appended is False
    assert await _count_products_for_source(source.id) == 0

    # The source row itself is untouched by this call (Phase 2: source
    # resolution happens once, up front, at run-creation time -- this
    # function never touches `sources` at all, see repository.py's module
    # docstring) -- still exists from create_source_run_task() above.
    async with get_session() as session:
        source_row = await session.execute(select(Source).where(Source.id == source.id))
        assert source_row.scalar_one_or_none() is not None
