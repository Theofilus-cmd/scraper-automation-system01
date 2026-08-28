"""`GET /products`, `GET /products/{id}`, `GET /products/{id}/history`.

This router is read-only. Every endpoint is scoped to the authenticated
user's current workspace through Product -> Source -> Workspace ownership.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from app.api.dependencies import CurrentWorkspace
from app.api.v1.pagination import (
    DEFAULT_LIMIT,
    clamp_limit,
    decode_cursor_or_422,
    paginated_response,
)
from app.api.v1.serializers import (
    current_observation_to_dict,
    observation_history_to_dict,
    product_to_dict,
)
from app.db.models.products_repository import (
    get_current_observation,
    get_product,
    list_product_history,
    list_products,
)
from app.db.session import get_session
from app.domain.errors import ProductNotFoundError

router = APIRouter(prefix="/products", tags=["products"])


@router.get("")
async def list_products_endpoint(
    current_workspace: CurrentWorkspace,
    source_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)

    async with get_session() as session:
        rows = await list_products(
            session,
            source_id=source_id,
            after=after,
            limit=effective_limit + 1,
            workspace_id=current_workspace.id,
        )

    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda product: (product.created_at, product.id),
        serialize=product_to_dict,
    )


@router.get("/{product_id}")
async def get_product_endpoint(
    product_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    async with get_session() as session:
        product = await get_product(
            session,
            product_id,
            workspace_id=current_workspace.id,
        )

        if product is None:
            raise ProductNotFoundError(product_id)

        observation = await get_current_observation(session, product_id)

    return {
        **product_to_dict(product),
        "current_observation": (
            current_observation_to_dict(observation) if observation is not None else None
        ),
    }


@router.get("/{product_id}/history")
async def list_product_history_endpoint(
    product_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)

    async with get_session() as session:
        product = await get_product(
            session,
            product_id,
            workspace_id=current_workspace.id,
        )

        if product is None:
            raise ProductNotFoundError(product_id)

        rows = await list_product_history(
            session,
            product_id,
            after=after,
            limit=effective_limit + 1,
            workspace_id=current_workspace.id,
        )

    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda history: (history.version_created_at, history.id),
        serialize=observation_history_to_dict,
    )
