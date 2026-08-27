"""Workspace-scoped source CRUD, scheduling, and manual run endpoints."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentWorkspace
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
    session: AsyncSession,
    source_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Return the latest product and observation for one source."""
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
async def create_source(
    body: CreateSourceRequest,
    response: Response,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    """Create or return a source in the authenticated user's workspace."""
    adapter = registry.detect(body.url)
    if adapter is None:
        raise UnsupportedAdapterError(body.url)

    async with get_session() as session:
        source, created = await create_or_get_source(
            session,
            workspace_id=current_workspace.id,
            url=body.url,
            adapter_slug=adapter.slug,
        )

    response.status_code = 201 if created else 200
    return source_to_dict(source)


@router.get("")
async def list_sources_endpoint(
    current_workspace: CurrentWorkspace,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List sources owned by the authenticated user's workspace."""
    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)

    async with get_session() as session:
        rows = await list_sources(
            session,
            workspace_id=current_workspace.id,
            status=status,
            after=after,
            limit=effective_limit + 1,
        )

    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda source: (source.created_at, source.id),
        serialize=source_to_dict,
    )


@router.get("/{source_id}")
async def get_source_endpoint(
    source_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    """Get one source owned by the authenticated user's workspace."""
    async with get_session() as session:
        source = await get_source(
            session,
            source_id,
            workspace_id=current_workspace.id,
        )
        if source is None:
            raise SourceNotFoundError(source_id)

        schedule = await get_schedule(
            session,
            source_id,
            workspace_id=current_workspace.id,
        )
        current_product = await _current_product_summary(session, source_id)

    return {
        **source_to_dict(source),
        "schedule": schedule_to_dict(schedule) if schedule is not None else None,
        "current_product": current_product,
    }


@router.patch("/{source_id}")
async def patch_source_endpoint(
    source_id: uuid.UUID,
    body: PatchSourceRequest,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    """Set an owned source to active or paused."""
    async with get_session() as session:
        source = await set_source_active_or_paused(
            session,
            source_id,
            body.status,
            workspace_id=current_workspace.id,
        )

    return source_to_dict(source)


@router.delete("/{source_id}")
async def delete_source_endpoint(
    source_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
) -> Response:
    """Archive an owned source and disable its schedule."""
    async with get_session() as session:
        await archive_source(
            session,
            source_id,
            workspace_id=current_workspace.id,
        )

    return Response(status_code=204)


@router.post("/{source_id}/unarchive")
async def unarchive_source_endpoint(
    source_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    """Unarchive an owned source."""
    async with get_session() as session:
        source = await unarchive_source(
            session,
            source_id,
            workspace_id=current_workspace.id,
        )

    return source_to_dict(source)


@router.put("/{source_id}/schedule")
async def upsert_schedule_endpoint(
    source_id: uuid.UUID,
    body: UpsertScheduleRequest,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    """Create or update the schedule of an owned source."""
    async with get_session() as session:
        schedule = await upsert_schedule(
            session,
            source_id,
            interval_minutes=body.interval_minutes,
            is_active=body.is_active,
            workspace_id=current_workspace.id,
        )

    return schedule_to_dict(schedule)


@router.delete("/{source_id}/schedule")
async def delete_schedule_endpoint(
    source_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
) -> Response:
    """Delete the schedule of an owned source."""
    async with get_session() as session:
        await delete_schedule(
            session,
            source_id,
            workspace_id=current_workspace.id,
        )

    return Response(status_code=204)


@router.post("/{source_id}/runs", status_code=202)
async def create_run_endpoint(
    source_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Start a manual run for an owned source."""
    run, task, _created = await create_manual_run(
        source_id=source_id,
        idempotency_key=idempotency_key,
        attach_to_in_flight=False,
        workspace_id=current_workspace.id,
    )
    task_id = task.id if task is not None else await first_task_id_for_run(run.id)

    return {
        "run_id": str(run.id),
        "task_id": str(task_id) if task_id is not None else None,
        "status": run.status,
    }