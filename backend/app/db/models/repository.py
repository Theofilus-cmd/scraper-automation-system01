"""upsert_scrape_result() -- doc 08 §4's write-gating rule (doc 17's Data
model section), the only place `sources` / `products` /
`current_observations` are ever written.

`sources` is unconditionally upserted on every call -- it's bookkeeping
("what URL did we try, with which adapter"), analogous to doc 05 §4's
`targets` table, which persists regardless of any single run's outcome.
The write-gating table only ever governs `products` + `current_observations`
(its "Valid" rows say "create/update PRODUCTS + CURRENT_OBSERVATIONS", not
sources) -- those two are the tables that hold data-quality claims, and
gating them is what "never overwrite a good snapshot with garbage" (doc 08
§4) actually means. When validation fails, this function stops right
after the `sources` upsert -- both the "no prior product" and "prior
product exists" rows of the write-gating table collapse to the same
action ("don't touch products/current_observations"), so there is no need
to branch on prior existence at all on the invalid path.

Idempotent throughout via `INSERT ... ON CONFLICT DO UPDATE`, so a repeat
scrape of the same identity is always safe to call again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.scraping import CurrentObservation, Product, Source
from app.db.session import get_session
from app.scraping.normalize import normalize_url
from app.scraping.types import NormalizedRecord, ValidationResult

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScrapeUpsertResult:
    """Everything the API layer needs to build either the 200 success body
    (doc 06 API contract's `{source, product, observation}`) or the 422
    error `details` on the invalid path.

    `source_id`/`source_url` are always populated (the `sources` upsert
    always runs). `product_*`/`observation` stay `None` when
    `is_valid` is `False` -- nothing was written for those.
    """

    is_valid: bool
    source_id: uuid.UUID
    source_url: str
    validation_errors: dict[str, str] = field(default_factory=dict)
    product_id: uuid.UUID | None = None
    product_url: str | None = None
    product_identity_key: str | None = None
    observation: dict[str, object] | None = None
    created: bool = False


def _compute_product_identity_key(record: NormalizedRecord) -> str:
    """doc 05 §6 / doc 10 §1 precedence: `sku`, else `product_id`, else
    normalized `product_url` (already normalized -- `normalize()` sets
    `NormalizedRecord.product_url` via `normalize_url()`).

    Prefixed by kind (`sku:`/`product_id:`/`url:`) to rule out a same-string
    collision between, say, one product's `sku` and an unrelated product's
    `product_id` -- not specified by the docs, a small, deliberate
    hardening flagged here rather than silently added.
    """
    if record.sku:
        return f"sku:{record.sku}"
    if record.product_id:
        return f"product_id:{record.product_id}"
    return f"url:{record.product_url}"


async def _upsert_source(
    session: AsyncSession, *, url: str, normalized_url: str, adapter_type: str
) -> uuid.UUID:
    table = Source.__table__
    stmt = (
        pg_insert(table)
        .values(url=url, normalized_url=normalized_url, adapter_type=adapter_type)
        .on_conflict_do_update(index_elements=["normalized_url"], set_={"url": url})
        .returning(table.c.id)
    )
    source_id: uuid.UUID = (await session.execute(stmt)).scalar_one()
    return source_id


async def _upsert_product(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    identity_key: str,
    product_url: str,
    sku: str | None,
    source_product_id: str | None,
) -> tuple[uuid.UUID, bool]:
    """Returns `(product_id, created)`. `created` uses Postgres's
    `xmax = 0` idiom (true only for a row this exact statement inserted,
    false when the `ON CONFLICT DO UPDATE` path fired instead) rather than
    a separate pre-check SELECT -- one round trip, no race window.
    """
    table = Product.__table__
    values = {
        "source_id": source_id,
        "product_identity_key": identity_key,
        "product_url": product_url,
        "sku": sku,
        "source_product_id": source_product_id,
    }
    stmt = pg_insert(table).values(**values)
    update_columns = {
        k: getattr(stmt.excluded, k) for k in ("product_url", "sku", "source_product_id")
    }
    stmt = stmt.on_conflict_do_update(
        constraint="uq_products_source_identity", set_=update_columns
    ).returning(table.c.id, text("(xmax = 0) AS created"))

    row = (await session.execute(stmt)).one()
    return row.id, bool(row.created)


async def _upsert_observation(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    record: NormalizedRecord,
    validation: ValidationResult,
) -> dict[str, object]:
    table = CurrentObservation.__table__
    values = {
        "product_id": product_id,
        "product_name": record.product_name,
        "brand": record.brand,
        "category": record.category,
        "price": record.price,
        "currency": record.currency,
        "original_price": record.original_price,
        "discount": record.discount,
        "variant": record.variant,
        "stock_status": record.stock_status,
        "rating": record.rating,
        "review_count": record.review_count,
        "description": record.description,
        "image_url": record.image_url,
        "is_valid": validation.is_valid,
        "validation_errors": validation.errors or None,
        "scraped_at": record.scraped_at,
    }
    stmt = pg_insert(table).values(**values)
    update_columns = {
        k: getattr(stmt.excluded, k) for k in values if k != "product_id"
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["product_id"], set_=update_columns
    ).returning(table)

    row = (await session.execute(stmt)).one()
    return {
        "product_name": row.product_name,
        "brand": row.brand,
        "category": row.category,
        "price": row.price,
        "currency": row.currency,
        "original_price": row.original_price,
        "discount": row.discount,
        "variant": row.variant,
        "stock_status": row.stock_status,
        "rating": row.rating,
        "review_count": row.review_count,
        "description": row.description,
        "image_url": row.image_url,
        "is_valid": row.is_valid,
        "validation_errors": row.validation_errors,
        "scraped_at": row.scraped_at,
    }


async def upsert_scrape_result(
    *,
    source_url: str,
    adapter_slug: str,
    record: NormalizedRecord,
    validation: ValidationResult,
) -> ScrapeUpsertResult:
    """The one entry point for persisting a scrape outcome. See module
    docstring for the write-gating rule this implements.
    """
    normalized_source_url = normalize_url(source_url)

    async with get_session() as session:
        source_id = await _upsert_source(
            session,
            url=source_url,
            normalized_url=normalized_source_url,
            adapter_type=adapter_slug,
        )

        if not validation.is_valid:
            await session.commit()
            logger.info(
                "scrape validation failed -- no product/observation row written",
                extra={"source_id": str(source_id), "errors": validation.errors},
            )
            return ScrapeUpsertResult(
                is_valid=False,
                source_id=source_id,
                source_url=source_url,
                validation_errors=validation.errors,
            )

        identity_key = _compute_product_identity_key(record)
        product_id, created = await _upsert_product(
            session,
            source_id=source_id,
            identity_key=identity_key,
            product_url=record.product_url,
            sku=record.sku,
            source_product_id=record.product_id,
        )
        observation = await _upsert_observation(
            session, product_id=product_id, record=record, validation=validation
        )

        await session.commit()
        logger.info(
            "scrape result persisted",
            extra={
                "source_id": str(source_id),
                "product_id": str(product_id),
                "created": created,
            },
        )

        return ScrapeUpsertResult(
            is_valid=True,
            source_id=source_id,
            source_url=source_url,
            validation_errors=validation.errors,
            product_id=product_id,
            product_url=record.product_url,
            product_identity_key=identity_key,
            observation=observation,
            created=created,
        )
