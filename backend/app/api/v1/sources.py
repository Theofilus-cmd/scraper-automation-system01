"""`/sources` CRUD, lifecycle (archive/unarchive/pause), schedule
upsert/delete, and the source-scoped manual run trigger -- doc 18 §6.1,
§6.2. `POST /sources/{id}/runs` is colocated here rather than in
`runs.py` since it acts on a source first and foremost ("trigger a run for
this source"); `runs.py` owns only the top-level `/runs` read paths.

Every domain exception a repository function below can raise (doc 18 §6.4)
is left to propagate -- `app/api/v1/error_mapping.py::domain_error_handler`
is registered globally (app/main.py) and translates it, so no handler here
needs its own `try/except DomainError`.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.pagination import (
    DEFAULT_LIMIT,
    clamp_limit,
    decode_cursor_or_422,
    paginated_response,
)
from app.api.v1.serializers import (
    current_observation_to_dict,
    product_to_dict,
    schedule_to_dict,
    source_to_dict,
)
from app.db.models.products_repository import get_current_observation, list_products
from app.db.models.runs_repository import create_manual_run, first_task_id_for_run
from app.db.models.sources_repository import (
    archive_source,
    create_or_get_source,
    delete_schedule,
    get_schedule,
    get_source,
    list_sources,
    set_source_active_or_paused,
    unarchive_source,
    upsert_schedule,
)
from app.db.session import get_session
from app.domain.errors import SourceNotFoundError, UnsupportedAdapterError
from app.scraping.adapters import registry

router = APIRouter(prefix="/sources", tags=["sources"])


class CreateSourceRequest(BaseModel):
    url: str = Field(min_length=1)


class PatchSourceRequest(BaseModel):
    status: Literal["active", "paused"]


class UpsertScheduleRequest(BaseModel):
    interval_minutes: int
    is_active: bool = True


async def _current_product_summary(
    session: AsyncSession, source_id: uuid.UUID
) -> dict[str, Any] | None:
    """doc 18 §6.1's `GET /sources/{id}` embeds a `current_product` summary,
    `null` if the source has never been scraped. Doc 18 doesn't spell out a
    shape for a source with more than one distinct product identity (only
    possible if a re-scrape resolves to a different sku/product_id/url than
    a prior scrape of the same source -- not expected in practice, since one
    source is one page today, doc 18 §9's no-multi-product-fan-out
    non-goal) -- this is a deliberate, flagged judgment call: the most
    recently created product for this source, with its current observation
    nested, or `None` if there is no product at all.
    """
    products = await list_products(session, source_id=source_id, after=None, limit=1)
    if not products:
        return None
    product = products[0]
    observation = await get_current_observation(session, product.id)
    return {
        "product": product_to_dict(product),
        "observation": (
            current_observation_to_dict(observation) if observation is not None else None
        ),
    }


@router.post("", status_code=200)
async def create_source(body: CreateSourceRequest, response: Response) -> dict[str, Any]:
    """doc 18 §6.1: idempotent on `normalized_url` among non-archived rows
    -- `200` with the existing match, or `201` with a freshly-created row.
    `SsrfBlockedError` (create-time check, §7.1) propagates from
    `create_or_get_source` to the global domain-error handler unchanged.
    """
    adapter = registry.detect(body.url)
    if adapter is None:
        raise UnsupportedAdapterError(body.url)

    async with get_session() as session:
        source, created = await create_or_get_source(
            session, url=body.url, adapter_slug=adapter.slug
        )
    response.status_code = 201 if created else 200
    return source_to_dict(source)


@router.get("")
async def list_sources_endpoint(
    status: str | None = None, cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)
    async with get_session() as session:
        rows = await list_sources(session, status=status, after=after, limit=effective_limit + 1)
    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda s: (s.created_at, s.id),
        serialize=source_to_dict,
    )


@router.get("/{source_id}")
async def get_source_endpoint(source_id: uuid.UUID) -> dict[str, Any]:
    async with get_session() as session:
        source = await get_source(session, source_id)
        if source is None:
            raise SourceNotFoundError(source_id)
        schedule = await get_schedule(session, source_id)
        current_product = await _current_product_summary(session, source_id)
    return {
        **source_to_dict(source),
        "schedule": schedule_to_dict(schedule) if schedule is not None else None,
        "current_product": current_product,
    }


@router.patch("/{source_id}")
async def patch_source_endpoint(source_id: uuid.UUID, body: PatchSourceRequest) -> dict[str, Any]:
    async with get_session() as session:
        source = await set_source_active_or_paused(session, source_id, body.status)
    return source_to_dict(source)


@router.delete("/{source_id}")
async def delete_source_endpoint(source_id: uuid.UUID) -> Response:
    """doc 18 §6.1/§3.1: archives, idempotent, `204` either way. Cascades
    to `schedules.is_active := false` inside `archive_source` itself
    (invariant I2)."""
    async with get_session() as session:
        await archive_source(session, source_id)
    return Response(status_code=204)


@router.post("/{source_id}/unarchive")
async def unarchive_source_endpoint(source_id: uuid.UUID) -> dict[str, Any]:
    async with get_session() as session:
        source = await unarchive_source(session, source_id)
    return source_to_dict(source)


@router.put("/{source_id}/schedule")
async def upsert_schedule_endpoint(
    source_id: uuid.UUID, body: UpsertScheduleRequest
) -> dict[str, Any]:
    async with get_session() as session:
        schedule = await upsert_schedule(
            session, source_id, interval_minutes=body.interval_minutes, is_active=body.is_active
        )
    return schedule_to_dict(schedule)


@router.delete("/{source_id}/schedule")
async def delete_schedule_endpoint(source_id: uuid.UUID) -> Response:
    """doc 18 §6.1: idempotent, `204` either way -- `delete_schedule`
    doesn't require the source to still exist, by its own design."""
    async with get_session() as session:
        await delete_schedule(session, source_id)
    return Response(status_code=204)


@router.post("/{source_id}/runs", status_code=202)
async def create_run_endpoint(
    source_id: uuid.UUID,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """doc 18 §6.2. `attach_to_in_flight=False`: a genuine collision with
    an already-in-flight run raises `RunInProgressError` ->
    `409 SOURCE_RUN_IN_PROGRESS` (the legacy alias, §6.6, is the only
    caller that ever passes `attach_to_in_flight=True`).

    `status` in the response reports the run's real, current status rather
    than doc 18 §6.2's illustrative `"pending"` literal: by the time this
    call returns, phase B (`runs_repository.py::_activate_run`) has already
    run synchronously within this same request for a freshly-created run,
    so its true status is already `"running"` -- reporting the live value
    is strictly more accurate and costs nothing extra, since the row was
    already loaded to build this response.
    """
    run, task, _created = await create_manual_run(
        source_id=source_id, idempotency_key=idempotency_key, attach_to_in_flight=False
    )
    task_id = task.id if task is not None else await first_task_id_for_run(run.id)
    return {
        "run_id": str(run.id),
        "task_id": str(task_id) if task_id is not None else None,
        "status": run.status,
    }
