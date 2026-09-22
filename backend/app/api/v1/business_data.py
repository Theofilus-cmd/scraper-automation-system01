"""Workspace-scoped API endpoints for My Business Hub business data."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentWorkspace
from app.api.v1.pagination import DEFAULT_LIMIT, clamp_limit, decode_cursor_or_422, paginated_response
from app.db.models.business_data_repository import (
    create_business_data_run,
    create_business_data_source,
    get_business_data_record,
    get_business_data_source,
    list_business_data_record_history,
    list_business_data_records,
    list_business_data_runs,
    list_business_data_sources,
)
from app.db.session import get_session
from app.domain.business_data_ingestion import (
    BusinessDataIngestionItem,
    ingest_business_data_records,
)

router = APIRouter(prefix="/business-data/sources", tags=["business-data"])

_SOURCE_TYPES = Literal["file_upload", "custom_api", "webhook"]
_ADAPTER_TYPES = Literal["csv", "xlsx", "http_json", "incoming_json"]
_DATA_TEMPLATES = Literal["universal_table", "product_inventory", "sales_orders"]
_TRIGGERED_BY = Literal["manual", "schedule", "upload", "webhook"]


class CreateBusinessDataSourceRequest(BaseModel):
    name: str = Field(min_length=1)
    source_type: _SOURCE_TYPES
    adapter_type: _ADAPTER_TYPES
    data_template: _DATA_TEMPLATES
    config: dict[str, object] = Field(default_factory=dict)


class IngestBusinessDataItemRequest(BaseModel):
    external_id: str
    fields: dict[str, object]
    captured_at: datetime


class IngestBusinessDataRequest(BaseModel):
    triggered_by: _TRIGGERED_BY = "manual"
    items: list[IngestBusinessDataItemRequest]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _source_to_dict(source: Any) -> dict[str, Any]:
    return {
        "id": str(source.id),
        "name": source.name,
        "source_type": source.source_type,
        "adapter_type": source.adapter_type,
        "status": source.status,
        "data_template": source.data_template,
        "config": source.config,
        "created_at": _iso(source.created_at),
        "updated_at": _iso(source.updated_at),
    }


def _run_to_dict(run: Any) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "source_id": str(run.business_data_source_id),
        "schedule_id": (
            str(run.business_data_schedule_id)
            if run.business_data_schedule_id is not None
            else None
        ),
        "status": run.status,
        "triggered_by": run.triggered_by,
        "total_records": run.total_records,
        "succeeded_records": run.succeeded_records,
        "failed_records": run.failed_records,
        "error_reason": run.error_reason,
        "error_detail": run.error_detail,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "created_at": _iso(run.created_at),
    }


def _record_to_dict(record: Any) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "source_id": str(record.business_data_source_id),
        "external_id": record.external_id,
        "fields": record.fields,
        "captured_at": _iso(record.captured_at),
        "last_run_id": str(record.last_run_id),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def _record_history_to_dict(history: Any) -> dict[str, Any]:
    return {
        "id": str(history.id),
        "record_id": str(history.business_data_record_id),
        "run_id": str(history.business_data_run_id),
        "fields": history.fields,
        "change_summary": history.change_summary,
        "captured_at": _iso(history.captured_at),
        "version_created_at": _iso(history.version_created_at),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_business_data_source_endpoint(
    body: CreateBusinessDataSourceRequest,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    """Create one business-data source in the authenticated workspace."""

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=current_workspace.id,
            name=body.name,
            source_type=body.source_type,
            adapter_type=body.adapter_type,
            data_template=body.data_template,
            config=body.config,
        )
        await session.commit()
        return _source_to_dict(source)


@router.get("")
async def list_business_data_sources_endpoint(
    current_workspace: CurrentWorkspace,
    status_filter: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List business-data sources owned by the authenticated workspace."""

    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)

    async with get_session() as session:
        rows = await list_business_data_sources(
            session,
            workspace_id=current_workspace.id,
            status=status_filter,
            after=after,
            limit=effective_limit + 1,
        )

    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda source: (source.created_at, source.id),
        serialize=_source_to_dict,
    )


@router.post("/{source_id}/ingest")
async def ingest_business_data_endpoint(
    source_id: uuid.UUID,
    body: IngestBusinessDataRequest,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    """Create a run and synchronously ingest normalized business-data items."""

    async with get_session() as session:
        source = await get_business_data_source(
            session,
            source_id,
            workspace_id=current_workspace.id,
        )
        if source is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Business data source not found.",
            )

        runs = await list_business_data_runs(
            session,
            source_id,
            workspace_id=current_workspace.id,
            after=None,
            limit=1,
        )
        if runs and runs[0].status in {"pending", "running"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Business data source already has a run in progress.",
            )

        try:
            run = await create_business_data_run(
                session,
                source_id,
                workspace_id=current_workspace.id,
                triggered_by=body.triggered_by,
            )
            if run is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Business data source not found.",
                )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Business data source already has a run in progress.",
            ) from None

    result = await ingest_business_data_records(
        run.id,
        workspace_id=current_workspace.id,
        items=[
            BusinessDataIngestionItem(
                external_id=item.external_id,
                fields=item.fields,
                captured_at=item.captured_at,
            )
            for item in body.items
        ],
    )

    return {
        "run_id": str(result.run_id),
        "status": result.status,
        "total_records": result.total_records,
        "succeeded_records": result.succeeded_records,
        "failed_records": result.failed_records,
        "errors": [
            {
                "external_id": error.external_id,
                "reason": error.reason,
            }
            for error in result.errors
        ],
    }


@router.get("/{source_id}/runs")
async def list_business_data_runs_endpoint(
    source_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List runs for one owned business-data source."""

    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)

    async with get_session() as session:
        source = await get_business_data_source(
            session,
            source_id,
            workspace_id=current_workspace.id,
        )
        if source is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Business data source not found.",
            )

        rows = await list_business_data_runs(
            session,
            source_id,
            workspace_id=current_workspace.id,
            after=after,
            limit=effective_limit + 1,
        )

    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda run: (run.created_at, run.id),
        serialize=_run_to_dict,
    )


@router.get("/{source_id}/records")
async def list_business_data_records_endpoint(
    source_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List current normalized records for one owned business-data source."""

    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)

    async with get_session() as session:
        source = await get_business_data_source(
            session,
            source_id,
            workspace_id=current_workspace.id,
        )
        if source is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Business data source not found.",
            )

        rows = await list_business_data_records(
            session,
            source_id,
            workspace_id=current_workspace.id,
            after=after,
            limit=effective_limit + 1,
        )

    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda record: (record.updated_at, record.id),
        serialize=_record_to_dict,
    )


@router.get("/{source_id}/records/{record_id}/history")
async def list_business_data_record_history_endpoint(
    source_id: uuid.UUID,
    record_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List append-only history snapshots for one owned business-data record."""

    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)

    async with get_session() as session:
        source = await get_business_data_source(
            session,
            source_id,
            workspace_id=current_workspace.id,
        )
        if source is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Business data source not found.",
            )

        record = await get_business_data_record(
            session,
            record_id,
            workspace_id=current_workspace.id,
        )
        if (
            record is None
            or record.business_data_source_id != source.id
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Business data record not found.",
            )

        rows = await list_business_data_record_history(
            session,
            record_id,
            workspace_id=current_workspace.id,
            after=after,
            limit=effective_limit + 1,
        )

    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda history: (history.version_created_at, history.id),
        serialize=_record_history_to_dict,
    )
