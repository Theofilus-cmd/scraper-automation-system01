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
