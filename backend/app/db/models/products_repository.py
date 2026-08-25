"""Read-only product / observation-history query functions (doc 18 §6.3) --
`GET /products`, `GET /products/{id}`, `GET /products/{id}/history`. Nothing
here ever writes: product/current-observation writes stay in
repository.py::upsert_scrape_result(); observation_history writes stay
there too, gated by the append condition (doc 18 §3.3).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.lifecycle import ObservationHistory
from app.db.models.scraping import CurrentObservation, Product


async def get_product(session: AsyncSession, product_id: uuid.UUID) -> Product | None:
    result: Product | None = (
        await session.execute(select(Product).where(Product.id == product_id))
    ).scalar_one_or_none()
    return result


async def get_current_observation(
    session: AsyncSession, product_id: uuid.UUID
) -> CurrentObservation | None:
    result: CurrentObservation | None = (
        await session.execute(
            select(CurrentObservation).where(CurrentObservation.product_id == product_id)
        )
    ).scalar_one_or_none()
    return result


async def list_products(
    session: AsyncSession,
    *,
    source_id: uuid.UUID | None,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[Product]:
    query = select(Product).order_by(Product.created_at.desc(), Product.id.desc()).limit(limit)
    if source_id is not None:
        query = query.where(Product.source_id == source_id)
    if after is not None:
        after_created_at, after_id = after
        query = query.where(
            (Product.created_at < after_created_at)
            | ((Product.created_at == after_created_at) & (Product.id < after_id))
        )
    return list((await session.execute(query)).scalars().all())


async def most_recent_product_for_source(
    session: AsyncSession, source_id: uuid.UUID
) -> Product | None:
    """Added alongside the API layer (commit 4): "the" product for a
    source, under the one-product-per-source-in-practice assumption doc 18
    §9 keeps in place (`_scrape_source_url_for_task` still only ever
    consumes `raw_records[0]`). Used by `GET /sources/{id}`'s
    `current_product` summary (app/api/v1/sources.py) and the legacy
    `POST /api/v1/scrapes` alias's response assembly (app/api/v1/scrapes.py,
    doc 18 §6.6) so both resolve it the same one way instead of duplicating
    the query. Reuses `list_products`'s existing `created_at DESC, id DESC`
    ordering rather than adding a second index for `updated_at` -- under
    the one-product assumption there is at most one row to find regardless
    of ordering, so no new index is needed for this to be correct.
    """
    rows = await list_products(session, source_id=source_id, after=None, limit=1)
    return rows[0] if rows else None


async def task_created_new_product(session: AsyncSession, task_id: uuid.UUID) -> bool:
    """Added alongside the API layer (commit 4): whether `task_id`'s own
    scrape is what created its product, vs. updating an already-existing
    one -- doc 17's `created` response field, which doc 18 §6.6 keeps.

    Not stored directly on `tasks` (migration 0003 gives it no such
    column), so recovered from `observation_history` instead, exactly, not
    as a heuristic: `_diff_against_prior_observation`'s contract
    (repository.py) guarantees `change_summary IS NULL` if and only if no
    prior `current_observations` row existed at upsert time. Because a
    product and its `current_observations` row are always created together
    in the same transaction and never independently
    (repository.py::upsert_scrape_result), "no prior row existed" and
    "this call's INSERT is what created the product" are the same fact --
    so a history row for this task_id with a NULL change_summary exists if
    and only if this task created its product. A task that only updated an
    already-existing product either appends no history row at all (no
    tracked field changed) or appends one with a non-null change_summary;
    neither is mistaken for "created" by this query.

    Unindexed on `task_id` (observation_history has no such index) -- an
    accepted, minor characteristic for a deprecated-endpoint convenience
    field (doc 18 §6.6 point 7) on a table doc 18 §7.5 already caps at a
    90-day retention window, not worth a new migration for at this stage.
    """
    row = (
        await session.execute(
            select(ObservationHistory.id).where(
                ObservationHistory.task_id == task_id,
                ObservationHistory.change_summary.is_(None),
            )
        )
    ).first()
    return row is not None


async def list_product_history(
    session: AsyncSession,
    product_id: uuid.UUID,
    *,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[ObservationHistory]:
    """Newest first (doc 18 §6.3) -- ordered by `version_created_at DESC`,
    matching `ix_observation_history_product_time`'s column order exactly
    so this query can use that index directly.
    """
    query = (
        select(ObservationHistory)
        .where(ObservationHistory.product_id == product_id)
        .order_by(ObservationHistory.version_created_at.desc(), ObservationHistory.id.desc())
        .limit(limit)
    )
    if after is not None:
        after_created_at, after_id = after
        query = query.where(
            (ObservationHistory.version_created_at < after_created_at)
            | (
                (ObservationHistory.version_created_at == after_created_at)
                & (ObservationHistory.id < after_id)
            )
        )
    return list((await session.execute(query)).scalars().all())
