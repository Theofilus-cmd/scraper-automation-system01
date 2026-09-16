"""Workspace-scoped repository functions for My Business Hub data sources."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.business_data import BusinessDataSource
from app.db.models.business_data import (
    BUSINESS_DATA_SCHEDULE_MAX_INTERVAL_MINUTES,
    BUSINESS_DATA_SCHEDULE_MIN_INTERVAL_MINUTES,
    BusinessDataSchedule,
)


async def create_business_data_source(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    name: str,
    source_type: str,
    adapter_type: str,
    data_template: str,
    config: dict[str, object] | None = None,
) -> BusinessDataSource:
    """Create one workspace-owned business data source."""

    source = BusinessDataSource(
        workspace_id=workspace_id,
        name=name,
        source_type=source_type,
        adapter_type=adapter_type,
        data_template=data_template,
        config=config or {},
    )
    session.add(source)
    await session.flush()
    await session.refresh(source)
    return source


async def get_business_data_source(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
) -> BusinessDataSource | None:
    """Return a business data source only when owned by the workspace."""

    return (
        await session.execute(
            select(BusinessDataSource).where(
                BusinessDataSource.id == source_id,
                BusinessDataSource.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()


async def list_business_data_sources(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    status: str | None,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[BusinessDataSource]:
    """List one workspace's business data sources, newest first."""

    query = (
        select(BusinessDataSource)
        .where(BusinessDataSource.workspace_id == workspace_id)
        .order_by(BusinessDataSource.created_at.desc(), BusinessDataSource.id.desc())
        .limit(limit)
    )

    if status is not None:
        query = query.where(BusinessDataSource.status == status)

    if after is not None:
        after_created_at, after_id = after
        query = query.where(
            (BusinessDataSource.created_at < after_created_at)
            | (
                (BusinessDataSource.created_at == after_created_at)
                & (BusinessDataSource.id < after_id)
            )
        )

    return list((await session.execute(query)).scalars().all())


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def get_business_data_schedule(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
) -> BusinessDataSchedule | None:
    """Return a source schedule only when the source belongs to the workspace."""

    return (
        await session.execute(
            select(BusinessDataSchedule)
            .join(
                BusinessDataSource,
                BusinessDataSource.id
                == BusinessDataSchedule.business_data_source_id,
            )
            .where(
                BusinessDataSchedule.business_data_source_id == source_id,
                BusinessDataSource.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()


async def upsert_business_data_schedule(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    interval_minutes: int,
    is_active: bool,
) -> BusinessDataSchedule | None:
    """Create or update the one schedule for a workspace-owned data source."""

    if interval_minutes < BUSINESS_DATA_SCHEDULE_MIN_INTERVAL_MINUTES:
        raise ValueError(
            "interval_minutes must be at least "
            f"{BUSINESS_DATA_SCHEDULE_MIN_INTERVAL_MINUTES}"
        )
    if interval_minutes > BUSINESS_DATA_SCHEDULE_MAX_INTERVAL_MINUTES:
        raise ValueError(
            "interval_minutes must be at most "
            f"{BUSINESS_DATA_SCHEDULE_MAX_INTERVAL_MINUTES}"
        )

    source = await get_business_data_source(
        session,
        source_id,
        workspace_id=workspace_id,
    )
    if source is None:
        return None

    schedule = await get_business_data_schedule(
        session,
        source_id,
        workspace_id=workspace_id,
    )
    now = _utcnow()

    if schedule is None:
        schedule = BusinessDataSchedule(
            business_data_source_id=source.id,
            interval_minutes=interval_minutes,
            is_active=is_active,
            next_run_at=(
                now + timedelta(minutes=interval_minutes) if is_active else now
            ),
        )
        session.add(schedule)
    else:
        reactivating = is_active and not schedule.is_active
        schedule.interval_minutes = interval_minutes
        schedule.is_active = is_active

        if reactivating:
            schedule.next_run_at = now + timedelta(minutes=interval_minutes)

    await session.flush()
    await session.refresh(schedule)
    return schedule
