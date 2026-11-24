"""`/product-matches` -- link internal products to scraped competitor products."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.dependencies import CurrentWorkspace
from app.api.v1.pagination import (
    DEFAULT_LIMIT,
    clamp_limit,
    decode_cursor_or_422,
    paginated_response,
)
from app.api.v1.serializers import match_to_dict
from app.core.errors import ApiError
from app.db.models.intelligence import MATCH_TYPES
from app.db.models.intelligence_repository import (
    auto_match_by_sku,
    competitor_rows,
    create_match,
    delete_match,
    find_workspace_product_by_url,
    get_internal_product,
    get_match,
    list_matches,
)
from app.db.session import get_session
from app.domain.errors import (
    DomainError,
    DuplicateProductMatchError,
    InternalProductNotFoundError,
    ProductMatchNotFoundError,
    ProductNotFoundError,
)
from app.scraping.normalize import normalize_url

router = APIRouter(prefix="/product-matches", tags=["product-matches"])


class CreateMatchRequest(BaseModel):
    """Exactly one of `product_id` (manual match) or `product_url` (url match)."""

    model_config = ConfigDict(extra="forbid")

    internal_product_id: uuid.UUID
    product_id: uuid.UUID | None = None
    product_url: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _exactly_one(self) -> CreateMatchRequest:
        if (self.product_id is None) == (self.product_url is None):
            raise ValueError("Provide exactly one of product_id or product_url.")
        return self


class AutoMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_product_id: uuid.UUID | None = None


def _translate(exc: DomainError) -> ApiError:
    if isinstance(exc, InternalProductNotFoundError):
        return ApiError(404, "INTERNAL_PRODUCT_NOT_FOUND", "No internal product with this id.")
    if isinstance(exc, ProductNotFoundError):
        return ApiError(404, "PRODUCT_NOT_FOUND", "No scraped product found for this reference.")
    if isinstance(exc, ProductMatchNotFoundError):
        return ApiError(404, "PRODUCT_MATCH_NOT_FOUND", "No product match with this id.")
    if isinstance(exc, DuplicateProductMatchError):
        return ApiError(409, "DUPLICATE_PRODUCT_MATCH", "These products are already matched.")
    raise exc


def _validated_url(raw: str) -> str:
    parsed = urlparse(raw.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ApiError(422, "INVALID_URL", "product_url must be an absolute http(s) URL.")
    return normalize_url(raw)


async def _serialized(session: Any, workspace_id: uuid.UUID, match_id: uuid.UUID) -> dict[str, Any]:
    match = await get_match(session, match_id, workspace_id=workspace_id)
    if match is None:  # pragma: no cover - just created/read in this request
        raise _translate(ProductMatchNotFoundError(match_id))
    rows = await competitor_rows(
        session,
        workspace_id=workspace_id,
        internal_product_ids=[match.internal_product_id],
        match_id=match_id,
    )
    return match_to_dict(match, rows[0] if rows else None)


@router.post("", status_code=201)
async def create_match_endpoint(
    body: CreateMatchRequest, current_workspace: CurrentWorkspace
) -> dict[str, Any]:
    workspace_id = current_workspace.id
    async with get_session() as session:
        try:
            if body.product_url is not None:
                # Validate the internal product first so a 404 there is not masked.
                if (
                    await get_internal_product(
                        session, body.internal_product_id, workspace_id=workspace_id
                    )
                    is None
                ):
                    raise InternalProductNotFoundError(body.internal_product_id)
                product = await find_workspace_product_by_url(
                    session, _validated_url(body.product_url), workspace_id=workspace_id
                )
                if product is None:
                    raise ApiError(
                        404,
                        "PRODUCT_NOT_FOUND",
                        "No scraped product in this workspace has this URL.",
                    )
                product_id, match_type = product.id, "url"
            else:
                assert body.product_id is not None  # guaranteed by the validator
                product_id, match_type = body.product_id, "manual"
            match = await create_match(
                session,
                workspace_id=workspace_id,
                internal_product_id=body.internal_product_id,
                product_id=product_id,
                match_type=match_type,
            )
        except DomainError as exc:
            raise _translate(exc) from exc
        return await _serialized(session, workspace_id, match.id)


@router.post("/auto-match")
async def auto_match_endpoint(
    body: AutoMatchRequest, current_workspace: CurrentWorkspace
) -> dict[str, Any]:
    """Create SKU matches (case-insensitive) for active internal products."""
    async with get_session() as session:
        if body.internal_product_id is not None and (
            await get_internal_product(
                session, body.internal_product_id, workspace_id=current_workspace.id
            )
            is None
        ):
            raise _translate(InternalProductNotFoundError(body.internal_product_id))
        created = await auto_match_by_sku(
            session,
            workspace_id=current_workspace.id,
            internal_product_id=body.internal_product_id,
        )
    return {"created": created}


@router.get("")
async def list_matches_endpoint(
    current_workspace: CurrentWorkspace,
    internal_product_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    match_type: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    if match_type is not None and match_type not in MATCH_TYPES:
        raise ApiError(422, "INVALID_MATCH_TYPE", f"match_type must be one of {list(MATCH_TYPES)}.")
    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)
    async with get_session() as session:
        rows = await list_matches(
            session,
            workspace_id=current_workspace.id,
            internal_product_id=internal_product_id,
            product_id=product_id,
            match_type=match_type,
            after=after,
            limit=effective_limit + 1,
        )
        page = rows[:effective_limit]
        details = {
            row["match"].id: row
            for row in await competitor_rows(
                session,
                workspace_id=current_workspace.id,
                internal_product_ids=list({m.internal_product_id for m in page}),
            )
        }
    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda m: (m.created_at, m.id),
        serialize=lambda m: match_to_dict(m, details.get(m.id)),
    )


@router.get("/{match_id}")
async def get_match_endpoint(
    match_id: uuid.UUID, current_workspace: CurrentWorkspace
) -> dict[str, Any]:
    async with get_session() as session:
        if await get_match(session, match_id, workspace_id=current_workspace.id) is None:
            raise _translate(ProductMatchNotFoundError(match_id))
        return await _serialized(session, current_workspace.id, match_id)


@router.delete("/{match_id}", status_code=204)
async def delete_match_endpoint(
    match_id: uuid.UUID, current_workspace: CurrentWorkspace
) -> Response:
    async with get_session() as session:
        try:
            await delete_match(session, match_id, workspace_id=current_workspace.id)
        except ProductMatchNotFoundError as exc:
            raise _translate(exc) from exc
    return Response(status_code=204)
