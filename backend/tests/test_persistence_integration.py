"""Direct upsert_scrape_result() tests -- doc 08 §4's write-gating rule
(doc 17's Data model section), against a real Postgres:

    docker compose exec api pytest -m integration

Deliberately bypasses HTTP/Celery entirely (unlike
test_scrapes_integration.py) so it can exercise the one case that
genuinely needs the SAME product identity scraped twice with different
outcomes (valid, then invalid) -- not reachable through the mock-store
HTTP fixtures alone, since each of mock-store's 4 slugs is a distinct
product identity.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models.repository import upsert_scrape_result
from app.db.models.scraping import CurrentObservation, Product, Source
from app.db.session import get_session
from app.scraping.types import NormalizedRecord, ValidationResult

pytestmark = pytest.mark.integration


def _unique_source_url() -> str:
    return f"http://mock-store:4000/products/test-{uuid.uuid4().hex}"


def _valid_record(
    source_url: str, *, price: str = "19.99", sku: str = "TEST-SKU-1"
) -> NormalizedRecord:
    return NormalizedRecord(
        product_url=source_url,
        scraped_at=datetime.now(UTC),
        stock_status="in_stock",
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


async def test_first_sighting_valid_creates_product_and_observation() -> None:
    source_url = _unique_source_url()
    validation = ValidationResult(is_valid=True, errors={})

    result = await upsert_scrape_result(
        source_url=source_url,
        adapter_slug="mock_store",
        record=_valid_record(source_url),
        validation=validation,
    )

    assert result.is_valid is True
    assert result.created is True
    assert result.product_id is not None
    assert result.observation is not None
    assert result.observation["price"] == Decimal("19.99")

    observation = await _fetch_observation(result.product_id)
    assert observation is not None
    assert observation.price == Decimal("19.99")
    assert observation.is_valid is True
    # doc 17 hotfix regression: this is the exact scenario that raised
    # asyncpg.exceptions.DataError ("can't subtract offset-naive and
    # offset-aware datetimes") before app/db/models/base.py's
    # type_annotation_map fix -- a first-ever valid write, whose
    # scraped_at (_valid_record() below uses datetime.now(UTC)) is
    # timezone-aware. A real round trip through Postgres must hand the
    # same tz-aware value back, not silently drop the offset.
    assert observation.scraped_at.tzinfo is not None


async def test_repeat_valid_scrape_updates_in_place_not_duplicated() -> None:
    source_url = _unique_source_url()
    validation = ValidationResult(is_valid=True, errors={})

    first = await upsert_scrape_result(
        source_url=source_url,
        adapter_slug="mock_store",
        record=_valid_record(source_url, price="19.99"),
        validation=validation,
    )
    second = await upsert_scrape_result(
        source_url=source_url,
        adapter_slug="mock_store",
        record=_valid_record(source_url, price="24.99"),
        validation=validation,
    )

    assert first.created is True
    assert second.created is False
    assert first.product_id == second.product_id
    assert await _count_products_for_source(first.source_id) == 1

    observation = await _fetch_observation(second.product_id)
    assert observation is not None
    assert observation.price == Decimal("24.99")


async def test_invalid_write_after_valid_leaves_prior_snapshot_standing() -> None:
    """The one case that genuinely needs the same identity scraped twice
    with different outcomes -- doc 08 §4's core guarantee, and the reason
    this file exists separately from the mock-store HTTP fixtures.
    """
    source_url = _unique_source_url()

    valid_result = await upsert_scrape_result(
        source_url=source_url,
        adapter_slug="mock_store",
        record=_valid_record(source_url, price="19.99"),
        validation=ValidationResult(is_valid=True, errors={}),
    )
    assert valid_result.product_id is not None

    invalid_result = await upsert_scrape_result(
        source_url=source_url,
        adapter_slug="mock_store",
        record=_invalid_record(source_url),
        validation=ValidationResult(
            is_valid=False, errors={"price": "missing required field: price"}
        ),
    )

    assert invalid_result.is_valid is False
    assert invalid_result.product_id is None
    assert invalid_result.observation is None

    # The prior valid snapshot must be untouched -- doc 08 §4's central
    # guarantee, and the one thing single-slug HTTP fixtures cannot prove.
    observation = await _fetch_observation(valid_result.product_id)
    assert observation is not None
    assert observation.price == Decimal("19.99")
    assert observation.is_valid is True

    assert await _count_products_for_source(valid_result.source_id) == 1


async def test_first_sighting_invalid_creates_no_product_or_observation() -> None:
    source_url = _unique_source_url()
    validation = ValidationResult(is_valid=False, errors={"price": "missing required field: price"})

    result = await upsert_scrape_result(
        source_url=source_url,
        adapter_slug="mock_store",
        record=_invalid_record(source_url),
        validation=validation,
    )

    assert result.is_valid is False
    assert result.product_id is None
    assert result.observation is None
    assert await _count_products_for_source(result.source_id) == 0

    # sources IS still created -- bookkeeping, see repository.py's module
    # docstring -- but zero products/current_observations for this
    # identity is the actual write-gating guarantee under test here.
    async with get_session() as session:
        source_row = await session.execute(select(Source).where(Source.id == result.source_id))
        assert source_row.scalar_one_or_none() is not None
