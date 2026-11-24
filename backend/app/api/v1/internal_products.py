"""`/internal-products` -- the workspace's own product catalog (Phase 4)."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.api.dependencies import CurrentWorkspace
from app.api.v1.pagination import (
    DEFAULT_LIMIT,
    clamp_limit,
    decode_cursor_or_422,
    paginated_response,
)
from app.api.v1.serializers import internal_product_to_dict
from app.core.errors import ApiError
from app.db.models.intelligence import INTERNAL_PRODUCT_STATUSES
from app.db.models.intelligence_repository import (
    create_internal_product,
    get_internal_product,
    list_internal_products,
    update_internal_product,
)
from app.db.session import get_session
from app.domain.errors import DuplicateSkuError, InternalProductNotFoundError

router = APIRouter(prefix="/internal-products", tags=["internal-products"])

_BARCODE_RE = re.compile(r"^\d{8,14}$")
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Money = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2, allow_inf_nan=False)]


class _ProductFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("barcode", check_fields=False)
    @classmethod
    def _barcode(cls, value: str | None) -> str | None:
        if value is not None and not _BARCODE_RE.match(value):
            raise ValueError("barcode must be 8-14 digits (GTIN).")
        return value

    @field_validator("currency", check_fields=False)
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _CURRENCY_RE.match(value):
            raise ValueError("currency must be a 3-letter ISO-4217 code.")
        return value.upper()


class CreateInternalProductRequest(_ProductFields):
    name: Name
    sku: Sku | None = None
    barcode: str | None = None
    brand: ShortText | None = None
    category: ShortText | None = None
    own_price: Money
    map_price: Money | None = None
    currency: str
    status: Literal["active", "archived"] = "active"


class PatchInternalProductRequest(_ProductFields):
    name: Name | None = None
    sku: Sku | None = None
    barcode: str | None = None
    brand: ShortText | None = None
    category: ShortText | None = None
    own_price: Money | None = None
    map_price: Money | None = None
    currency: str | None = None
    status: Literal["active", "archived"] | None = None


_NOT_NULLABLE = ("name", "own_price", "currency", "status")


def _translate(exc: Exception) -> ApiError:
    if isinstance(exc, DuplicateSkuError):
        return ApiError(409, "DUPLICATE_SKU", "A product with this SKU already exists.")
    if isinstance(exc, InternalProductNotFoundError):
        return ApiError(404, "INTERNAL_PRODUCT_NOT_FOUND", "No internal product with this id.")
    raise exc


@router.post("", status_code=201)
async def create_internal_product_endpoint(
    body: CreateInternalProductRequest, current_workspace: CurrentWorkspace
) -> dict[str, Any]:
    async with get_session() as session:
        try:
            item = await create_internal_product(
                session, workspace_id=current_workspace.id, values=body.model_dump()
            )
        except DuplicateSkuError as exc:
            raise _translate(exc) from exc
    return internal_product_to_dict(item)


@router.get("")
async def list_internal_products_endpoint(
    current_workspace: CurrentWorkspace,
    status: str | None = None,
    sku: str | None = None,
    q: Annotated[str | None, Field(max_length=200)] = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    if status is not None and status not in INTERNAL_PRODUCT_STATUSES:
        raise ApiError(
            422, "INVALID_STATUS", f"status must be one of {list(INTERNAL_PRODUCT_STATUSES)}."
        )
    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)
    async with get_session() as session:
        rows = await list_internal_products(
            session,
            workspace_id=current_workspace.id,
            status=status,
            sku=sku,
            q=q,
            after=after,
            limit=effective_limit + 1,
        )
    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda item: (item.created_at, item.id),
        serialize=internal_product_to_dict,
    )


@router.get("/{internal_product_id}")
async def get_internal_product_endpoint(
    internal_product_id: uuid.UUID, current_workspace: CurrentWorkspace
) -> dict[str, Any]:
    async with get_session() as session:
        item = await get_internal_product(
            session, internal_product_id, workspace_id=current_workspace.id
        )
    if item is None:
        raise _translate(InternalProductNotFoundError(internal_product_id))
    return internal_product_to_dict(item)


@router.patch("/{internal_product_id}")
async def patch_internal_product_endpoint(
    internal_product_id: uuid.UUID,
    body: PatchInternalProductRequest,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True)
    if not values:
        raise ApiError(422, "EMPTY_PATCH", "Provide at least one field to update.")
    for key in _NOT_NULLABLE:
        if key in values and values[key] is None:
            raise ApiError(422, "INVALID_FIELD", f"{key} cannot be null.", {"field": key})
    async with get_session() as session:
        try:
            item = await update_internal_product(
                session, internal_product_id, workspace_id=current_workspace.id, values=values
            )
        except (DuplicateSkuError, InternalProductNotFoundError) as exc:
            raise _translate(exc) from exc
    return internal_product_to_dict(item)


@router.delete("/{internal_product_id}", status_code=204)
async def archive_internal_product_endpoint(
    internal_product_id: uuid.UUID, current_workspace: CurrentWorkspace
) -> Response:
    """Soft delete: sets status=archived (matches are kept)."""
    async with get_session() as session:
        try:
            await update_internal_product(
                session,
                internal_product_id,
                workspace_id=current_workspace.id,
                values={"status": "archived"},
            )
        except InternalProductNotFoundError as exc:
            raise _translate(exc) from exc
    return Response(status_code=204)
