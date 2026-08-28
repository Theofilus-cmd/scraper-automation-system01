"""Read-only product / observation-history query functions (doc 18 §6.3).

Products do not store a workspace_id directly. Ownership is enforced through
the Product -> Source -> Workspace relationship whenever workspace_id is
provided by an HTTP-facing caller.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.lifecycle import ObservationHistory
from app.db.models.scraping import CurrentObservation, Product, Source


async def get_product(
    session: AsyncSession,
    product_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Product | None:
    """Return one product, optionally requiring source workspace ownership."""
    query = select(Product).where(Product.id == product_id)

    if workspace_id is not None:
        query = query.join(Source, Source.id == Product.source_id).where(
            Source.workspace_id == workspace_id
        )

    return (await session.execute(query)).scalar_one_or_none()


async def get_current_observation(
    session: AsyncSession,
    product_id: uuid.UUID,
) -> CurrentObservation | None:
    """Return the current observation for a product already ownership-checked."""
    return (
        await session.execute(
            select(CurrentObservation).where(CurrentObservation.product_id == product_id)
        )
    ).scalar_one_or_none()


async def list_products(
    session: AsyncSession,
    *,
    source_id: uuid.UUID | None,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
    workspace_id: uuid.UUID | None = None,
) -> list[Product]:
    """List products newest first, optionally limited to one workspace."""
    query = select(Product)

    if workspace_id is not None:
        query = query.join(Source, Source.id == Product.source_id).where(
            Source.workspace_id == workspace_id
        )

    if source_id is not None:
        query = query.where(Product.source_id == source_id)

    if after is not None:
        after_created_at, after_id = after
        query = query.where(
            (Product.created_at < after_created_at)
            | ((Product.created_at == after_created_at) & (Product.id < after_id))
        )

    query = query.order_by(Product.created_at.desc(), Product.id.desc()).limit(limit)

    return list((await session.execute(query)).scalars().all())


async def most_recent_product_for_source(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Product | None:
    """Return the newest product for a source, optionally workspace-scoped."""
    rows = await list_products(
        session,
        source_id=source_id,
        after=None,
        limit=1,
        workspace_id=workspace_id,
    )
    return rows[0] if rows else None


async def task_created_new_product(
    session: AsyncSession,
    task_id: uuid.UUID,
) -> bool:
    """Return whether task_id created the product rather than updating one."""
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
    workspace_id: uuid.UUID | None = None,
) -> list[ObservationHistory]:
    """List history newest first, optionally requiring product workspace ownership."""
    query = select(ObservationHistory).where(
        ObservationHistory.product_id == product_id
    )

    if workspace_id is not None:
        query = (
            query.join(Product, Product.id == ObservationHistory.product_id)
            .join(Source, Source.id == Product.source_id)
            .where(Source.workspace_id == workspace_id)
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

    query = query.order_by(
        ObservationHistory.version_created_at.desc(),
        ObservationHistory.id.desc(),
    ).limit(limit)

    return list((await session.execute(query)).scalars().all())
