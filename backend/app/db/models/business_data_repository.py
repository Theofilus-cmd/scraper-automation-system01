"""Workspace-scoped repository functions for My Business Hub data sources."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.business_data import (
    BUSINESS_DATA_RUN_TRIGGERED_BY,
    BUSINESS_DATA_SCHEDULE_MAX_INTERVAL_MINUTES,
    BUSINESS_DATA_SCHEDULE_MIN_INTERVAL_MINUTES,
    BusinessDataRecord,
    BusinessDataRecordHistory,
    BusinessDataRun,
    BusinessDataSchedule,
    BusinessDataSource,
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

BUSINESS_DATA_RUN_TERMINAL_STATUSES = (
    "completed",
    "completed_with_errors",
    "failed",
    "cancelled",
)


async def create_business_data_run(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    triggered_by: str,
    business_data_schedule_id: uuid.UUID | None = None,
    client_idempotency_key: str | None = None,
) -> BusinessDataRun | None:
    """Buat run berstatus pending untuk source milik workspace."""

    if triggered_by not in BUSINESS_DATA_RUN_TRIGGERED_BY:
        raise ValueError(
            "triggered_by harus salah satu dari "
            f"{', '.join(BUSINESS_DATA_RUN_TRIGGERED_BY)}"
        )

    source = await get_business_data_source(
        session,
        source_id,
        workspace_id=workspace_id,
    )
    if source is None:
        return None

    if business_data_schedule_id is not None:
        schedule = await get_business_data_schedule(
            session,
            source_id,
            workspace_id=workspace_id,
        )
        if schedule is None or schedule.id != business_data_schedule_id:
            return None

    run = BusinessDataRun(
        business_data_source_id=source.id,
        business_data_schedule_id=business_data_schedule_id,
        status="pending",
        triggered_by=triggered_by,
        client_idempotency_key=client_idempotency_key,
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)
    return run


async def get_business_data_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
) -> BusinessDataRun | None:
    """Ambil run hanya jika source-nya milik workspace tersebut."""

    return (
        await session.execute(
            select(BusinessDataRun)
            .join(
                BusinessDataSource,
                BusinessDataSource.id == BusinessDataRun.business_data_source_id,
            )
            .where(
                BusinessDataRun.id == run_id,
                BusinessDataSource.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()


async def list_business_data_runs(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[BusinessDataRun]:
    """List run sebuah source milik workspace, dari yang terbaru."""

    source = await get_business_data_source(
        session,
        source_id,
        workspace_id=workspace_id,
    )
    if source is None:
        return []

    query = (
        select(BusinessDataRun)
        .where(BusinessDataRun.business_data_source_id == source.id)
        .order_by(BusinessDataRun.created_at.desc(), BusinessDataRun.id.desc())
        .limit(limit)
    )

    if after is not None:
        after_created_at, after_id = after
        query = query.where(
            (BusinessDataRun.created_at < after_created_at)
            | (
                (BusinessDataRun.created_at == after_created_at)
                & (BusinessDataRun.id < after_id)
            )
        )

    return list((await session.execute(query)).scalars().all())


async def mark_business_data_run_running(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
) -> BusinessDataRun | None:
    """Ubah run pending milik workspace menjadi running."""

    run = await get_business_data_run(
        session,
        run_id,
        workspace_id=workspace_id,
    )
    if run is None or run.status != "pending":
        return None

    run.status = "running"
    run.started_at = _utcnow()
    await session.flush()
    await session.refresh(run)
    return run


async def finish_business_data_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    status: str,
    total_records: int,
    succeeded_records: int,
    failed_records: int,
    error_reason: str | None = None,
    error_detail: dict[str, object] | None = None,
) -> BusinessDataRun | None:
    """Selesaikan run running milik workspace dengan hasil akhir."""

    if status not in BUSINESS_DATA_RUN_TERMINAL_STATUSES:
        raise ValueError(
            "status harus salah satu dari "
            f"{', '.join(BUSINESS_DATA_RUN_TERMINAL_STATUSES)}"
        )

    run = await get_business_data_run(
        session,
        run_id,
        workspace_id=workspace_id,
    )
    if run is None or run.status != "running":
        return None
    run.status = status
    run.total_records = total_records
    run.succeeded_records = succeeded_records
    run.failed_records = failed_records
    run.error_reason = error_reason
    run.error_detail = error_detail
    run.finished_at = _utcnow()
    await session.flush()
    await session.refresh(run)
    return run


def _business_data_fields_diff(
    previous_fields: dict[str, object],
    current_fields: dict[str, object],
) -> dict[str, dict[str, object]]:
    """Return per-field before/after values for a changed record."""

    changes: dict[str, dict[str, object]] = {}

    for field_name in sorted(set(previous_fields) | set(current_fields)):
        previous_value = previous_fields.get(field_name)
        current_value = current_fields.get(field_name)

        if previous_value != current_value:
            changes[field_name] = {
                "before": previous_value,
                "after": current_value,
            }

    return changes


async def get_business_data_record(
    session: AsyncSession,
    record_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
) -> BusinessDataRecord | None:
    """Return a record only when its source belongs to the workspace."""

    return (
        await session.execute(
            select(BusinessDataRecord)
            .join(
                BusinessDataSource,
                BusinessDataSource.id
                == BusinessDataRecord.business_data_source_id,
            )
            .where(
                BusinessDataRecord.id == record_id,
                BusinessDataSource.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()


async def list_business_data_records(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[BusinessDataRecord]:
    """List current records of a workspace-owned source, newest first."""

    source = await get_business_data_source(
        session,
        source_id,
        workspace_id=workspace_id,
    )
    if source is None:
        return []

    query = (
        select(BusinessDataRecord)
        .where(BusinessDataRecord.business_data_source_id == source.id)
        .order_by(BusinessDataRecord.updated_at.desc(), BusinessDataRecord.id.desc())
        .limit(limit)
    )

    if after is not None:
        after_updated_at, after_id = after
        query = query.where(
            (BusinessDataRecord.updated_at < after_updated_at)
            | (
                (BusinessDataRecord.updated_at == after_updated_at)
                & (BusinessDataRecord.id < after_id)
            )
        )

    return list((await session.execute(query)).scalars().all())


async def list_business_data_record_history(
    session: AsyncSession,
    record_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[BusinessDataRecordHistory]:
    """List append-only history for a workspace-owned record, newest first."""

    record = await get_business_data_record(
        session,
        record_id,
        workspace_id=workspace_id,
    )
    if record is None:
        return []

    query = (
        select(BusinessDataRecordHistory)
        .where(BusinessDataRecordHistory.business_data_record_id == record.id)
        .order_by(
            BusinessDataRecordHistory.version_created_at.desc(),
            BusinessDataRecordHistory.id.desc(),
        )
        .limit(limit)
    )

    if after is not None:
        after_created_at, after_id = after
        query = query.where(
            (
                BusinessDataRecordHistory.version_created_at
                < after_created_at
            )
            | (
                (
                    BusinessDataRecordHistory.version_created_at
                    == after_created_at
                )
                & (BusinessDataRecordHistory.id < after_id)
            )
        )

    return list((await session.execute(query)).scalars().all())


async def upsert_business_data_record(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    external_id: str,
    fields: dict[str, object],
    captured_at: datetime,
) -> tuple[BusinessDataRecord, BusinessDataRecordHistory | None] | None:
    """Upsert a current record and append history only for new or changed fields."""

    source = await get_business_data_source(
        session,
        source_id,
        workspace_id=workspace_id,
    )
    if source is None:
        return None

    run = await get_business_data_run(
        session,
        run_id,
        workspace_id=workspace_id,
    )
    if run is None or run.business_data_source_id != source.id:
        return None

    record = (
        await session.execute(
            select(BusinessDataRecord)
            .where(
                BusinessDataRecord.business_data_source_id == source.id,
                BusinessDataRecord.external_id == external_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    history: BusinessDataRecordHistory | None = None

    if record is None:
        record = BusinessDataRecord(
            business_data_source_id=source.id,
            external_id=external_id,
            fields=fields,
            captured_at=captured_at,
            last_run_id=run.id,
        )
        session.add(record)
        await session.flush()

        history = BusinessDataRecordHistory(
            business_data_record_id=record.id,
            business_data_run_id=run.id,
            fields=fields,
            change_summary=None,
            captured_at=captured_at,
            version_created_at=_utcnow(),
        )
        session.add(history)
    else:
        changed = record.fields != fields
        change_summary = (
            _business_data_fields_diff(record.fields, fields)
            if changed
            else None
        )

        record.fields = fields
        record.captured_at = captured_at
        record.last_run_id = run.id
        record.updated_at = _utcnow()

        if changed:
            history = BusinessDataRecordHistory(
                business_data_record_id=record.id,
                business_data_run_id=run.id,
                fields=fields,
                change_summary=change_summary,
                captured_at=captured_at,
                version_created_at=_utcnow(),
            )
            session.add(history)

    await session.flush()
    await session.refresh(record)

    if history is not None:
        await session.refresh(history)

    return record, history
