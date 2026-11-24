"""`/price-intelligence` -- price gap, position and MAP checks (Phase 4)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from fastapi import APIRouter

from app.api.dependencies import CurrentWorkspace
from app.api.v1.pagination import (
    DEFAULT_LIMIT,
    clamp_limit,
    decode_cursor_or_422,
    paginated_response,
)
from app.api.v1.serializers import intelligence_to_dict
from app.core.errors import ApiError
from app.db.models.intelligence import InternalProduct
from app.db.models.intelligence_repository import (
    competitor_rows,
    get_internal_product,
    list_intelligence_products,
)
from app.db.session import get_session
from app.domain.price_intelligence import Competitor, IntelligenceResult, compute_intelligence

router = APIRouter(prefix="/price-intelligence", tags=["price-intelligence"])


def _compute(item: InternalProduct, rows: list[dict[str, Any]]) -> IntelligenceResult:
    competitors = []
    for row in rows:
        obs = row["observation"]
        competitors.append(
            Competitor(
                match_id=str(row["match"].id),
                price=obs.price if obs is not None else None,
                currency=obs.currency if obs is not None else None,
                is_valid=obs.is_valid if obs is not None else False,
            )
        )
    return compute_intelligence(
        own_price=item.own_price,
        map_price=item.map_price,
        currency=item.currency,
        competitors=competitors,
    )


@router.get("")
async def list_price_intelligence(
    current_workspace: CurrentWorkspace,
    map_violation: bool = False,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """One summary per active internal product that has matches.

    `map_violation=true` keeps only products with a MAP problem (a
    competitor below `map_price`, or own price below `map_price`).
    """
    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)
    async with get_session() as session:
        items = await list_intelligence_products(
            session,
            workspace_id=current_workspace.id,
            map_violation_only=map_violation,
            after=after,
            limit=effective_limit + 1,
        )
        page = items[:effective_limit]
        by_product: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
        for row in await competitor_rows(
            session,
            workspace_id=current_workspace.id,
            internal_product_ids=[i.id for i in page],
        ):
            by_product[row["match"].internal_product_id].append(row)
    return paginated_response(
        items,
        limit=effective_limit,
        cursor_of=lambda i: (i.created_at, i.id),
        serialize=lambda i: intelligence_to_dict(
            i, _compute(i, by_product[i.id]), by_product[i.id], detail=False
        ),
    )


@router.get("/{internal_product_id}")
async def get_price_intelligence(
    internal_product_id: uuid.UUID, current_workspace: CurrentWorkspace
) -> dict[str, Any]:
    async with get_session() as session:
        item = await get_internal_product(
            session, internal_product_id, workspace_id=current_workspace.id
        )
        if item is None:
            raise ApiError(404, "INTERNAL_PRODUCT_NOT_FOUND", "No internal product with this id.")
        rows = await competitor_rows(
            session, workspace_id=current_workspace.id, internal_product_ids=[item.id]
        )
    return intelligence_to_dict(item, _compute(item, rows), rows, detail=True)
