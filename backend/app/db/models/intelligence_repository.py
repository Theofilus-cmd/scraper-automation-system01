"""Workspace-scoped queries for internal products, matches and intelligence.

Every function takes `workspace_id` and filters on it; competitor `products`
carry no workspace_id, so ownership is enforced via Product -> Source.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.intelligence import InternalProduct, ProductMatch
from app.db.models.scraping import CurrentObservation, Product, Source
from app.domain.errors import (
    DuplicateProductMatchError,
    DuplicateSkuError,
    InternalProductNotFoundError,
    ProductMatchNotFoundError,
    ProductNotFoundError,
)

Cursor = tuple[datetime, uuid.UUID]


def _after(query: Any, model: Any, after: Cursor | None) -> Any:
    if after is None:
        return query
    created_at, id_ = after
    return query.where(
        (model.created_at < created_at) | ((model.created_at == created_at) & (model.id < id_))
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- internal products -----------------------------------------------------


async def create_internal_product(
    session: AsyncSession, *, workspace_id: uuid.UUID, values: dict[str, Any]
) -> InternalProduct:
    item = InternalProduct(workspace_id=workspace_id, **values)
    session.add(item)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateSkuError(str(values.get("sku"))) from exc
    await session.refresh(item)
    return item


async def get_internal_product(
    session: AsyncSession, internal_product_id: uuid.UUID, *, workspace_id: uuid.UUID
) -> InternalProduct | None:
    return (
        await session.execute(
            select(InternalProduct).where(
                InternalProduct.id == internal_product_id,
                InternalProduct.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()


async def list_internal_products(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    status: str | None,
    sku: str | None,
    q: str | None,
    after: Cursor | None,
    limit: int,
) -> list[InternalProduct]:
    query = select(InternalProduct).where(InternalProduct.workspace_id == workspace_id)
    if status is not None:
        query = query.where(InternalProduct.status == status)
    if sku is not None:
        query = query.where(func.lower(InternalProduct.sku) == sku.lower())
    if q:
        query = query.where(InternalProduct.name.ilike(f"%{_escape_like(q)}%", escape="\\"))
    query = _after(query, InternalProduct, after)
    query = query.order_by(InternalProduct.created_at.desc(), InternalProduct.id.desc())
    return list((await session.execute(query.limit(limit))).scalars().all())


async def update_internal_product(
    session: AsyncSession,
    internal_product_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    values: dict[str, Any],
) -> InternalProduct:
    item = await get_internal_product(session, internal_product_id, workspace_id=workspace_id)
    if item is None:
        raise InternalProductNotFoundError(internal_product_id)
    for key, value in values.items():
        setattr(item, key, value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateSkuError(str(values.get("sku"))) from exc
    await session.refresh(item)
    return item


# --- matches ---------------------------------------------------------------


async def get_workspace_product(
    session: AsyncSession, product_id: uuid.UUID, *, workspace_id: uuid.UUID
) -> Product | None:
    return (
        await session.execute(
            select(Product)
            .join(Source, Source.id == Product.source_id)
            .where(Product.id == product_id, Source.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()


async def find_workspace_product_by_url(
    session: AsyncSession, normalized_url: str, *, workspace_id: uuid.UUID
) -> Product | None:
    """Most recently created product with this normalized URL (if any)."""
    return (
        await session.execute(
            select(Product)
            .join(Source, Source.id == Product.source_id)
            .where(Product.product_url == normalized_url, Source.workspace_id == workspace_id)
            .order_by(Product.created_at.desc(), Product.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_match(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    internal_product_id: uuid.UUID,
    product_id: uuid.UUID,
    match_type: str,
) -> ProductMatch:
    if await get_internal_product(session, internal_product_id, workspace_id=workspace_id) is None:
        raise InternalProductNotFoundError(internal_product_id)
    if await get_workspace_product(session, product_id, workspace_id=workspace_id) is None:
        raise ProductNotFoundError(product_id)
    match = ProductMatch(
        workspace_id=workspace_id,
        internal_product_id=internal_product_id,
        product_id=product_id,
        match_type=match_type,
    )
    session.add(match)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateProductMatchError(internal_product_id, product_id) from exc
    await session.refresh(match)
    return match


async def get_match(
    session: AsyncSession, match_id: uuid.UUID, *, workspace_id: uuid.UUID
) -> ProductMatch | None:
    return (
        await session.execute(
            select(ProductMatch).where(
                ProductMatch.id == match_id, ProductMatch.workspace_id == workspace_id
            )
        )
    ).scalar_one_or_none()


async def list_matches(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    internal_product_id: uuid.UUID | None,
    product_id: uuid.UUID | None,
    match_type: str | None,
    after: Cursor | None,
    limit: int,
) -> list[ProductMatch]:
    query = select(ProductMatch).where(ProductMatch.workspace_id == workspace_id)
    if internal_product_id is not None:
        query = query.where(ProductMatch.internal_product_id == internal_product_id)
    if product_id is not None:
        query = query.where(ProductMatch.product_id == product_id)
    if match_type is not None:
        query = query.where(ProductMatch.match_type == match_type)
    query = _after(query, ProductMatch, after)
    query = query.order_by(ProductMatch.created_at.desc(), ProductMatch.id.desc())
    return list((await session.execute(query.limit(limit))).scalars().all())


async def delete_match(
    session: AsyncSession, match_id: uuid.UUID, *, workspace_id: uuid.UUID
) -> None:
    match = await get_match(session, match_id, workspace_id=workspace_id)
    if match is None:
        raise ProductMatchNotFoundError(match_id)
    await session.delete(match)
    await session.commit()


async def auto_match_by_sku(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    internal_product_id: uuid.UUID | None,
) -> int:
    """Create `sku` matches between active internal products and workspace
    products with the same SKU (case-insensitive). Idempotent; returns the
    number of newly created matches."""
    query = (
        select(InternalProduct.id, Product.id)
        .join(Product, func.lower(Product.sku) == func.lower(InternalProduct.sku))
        .join(Source, Source.id == Product.source_id)
        .where(
            InternalProduct.workspace_id == workspace_id,
            Source.workspace_id == workspace_id,
            InternalProduct.status == "active",
            InternalProduct.sku.is_not(None),
            Product.sku.is_not(None),
        )
    )
    if internal_product_id is not None:
        query = query.where(InternalProduct.id == internal_product_id)
    pairs = (await session.execute(query)).all()
    if not pairs:
        return 0
    stmt = (
        pg_insert(ProductMatch)
        .values(
            [
                {
                    "workspace_id": workspace_id,
                    "internal_product_id": internal_id,
                    "product_id": product_id,
                    "match_type": "sku",
                }
                for internal_id, product_id in pairs
            ]
        )
        .on_conflict_do_nothing(constraint="uq_product_matches_pair")
        .returning(ProductMatch.id)
    )
    created = len((await session.execute(stmt)).all())
    await session.commit()
    return created


# --- intelligence ----------------------------------------------------------


async def list_intelligence_products(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    map_violation_only: bool,
    after: Cursor | None,
    limit: int,
) -> list[InternalProduct]:
    """Active internal products that have at least one match (paginated)."""
    query = select(InternalProduct).where(
        InternalProduct.workspace_id == workspace_id,
        InternalProduct.status == "active",
        exists().where(ProductMatch.internal_product_id == InternalProduct.id),
    )
    if map_violation_only:
        violating = (
            exists()
            .where(
                ProductMatch.internal_product_id == InternalProduct.id,
                ProductMatch.product_id == CurrentObservation.product_id,
                CurrentObservation.is_valid.is_(True),
                CurrentObservation.currency == InternalProduct.currency,
                CurrentObservation.price < InternalProduct.map_price,
            )
            .correlate(InternalProduct)
        )
        query = query.where(
            InternalProduct.map_price.is_not(None),
            (InternalProduct.own_price < InternalProduct.map_price) | violating,
        )
    query = _after(query, InternalProduct, after)
    query = query.order_by(InternalProduct.created_at.desc(), InternalProduct.id.desc())
    return list((await session.execute(query.limit(limit))).scalars().all())


async def competitor_rows(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    internal_product_ids: list[uuid.UUID],
    match_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Matches joined with competitor product, source and current observation."""
    if not internal_product_ids:
        return []
    query = (
        select(ProductMatch, Product, Source, CurrentObservation)
        .join(Product, Product.id == ProductMatch.product_id)
        .join(Source, Source.id == Product.source_id)
        .outerjoin(CurrentObservation, CurrentObservation.product_id == Product.id)
        .where(
            ProductMatch.workspace_id == workspace_id,
            Source.workspace_id == workspace_id,
            ProductMatch.internal_product_id.in_(internal_product_ids),
        )
        .order_by(ProductMatch.created_at.asc(), ProductMatch.id.asc())
    )
    if match_id is not None:
        query = query.where(ProductMatch.id == match_id)
    rows = (await session.execute(query)).all()
    return [{"match": m, "product": p, "source": s, "observation": o} for m, p, s, o in rows]
