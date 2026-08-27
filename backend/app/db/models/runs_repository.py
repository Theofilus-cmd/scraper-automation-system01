"""Run and task repository functions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.lifecycle import RUN_IN_FLIGHT_STATUSES, Run, Task
from app.db.models.scraping import Source
from app.db.models.sources_repository import get_source
from app.db.session import get_session
from app.domain.errors import RunInProgressError, SourceArchivedError, SourceNotFoundError

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _task_idempotency_key(run_id: uuid.UUID, source_id: uuid.UUID) -> str:
    return f"{run_id}:{source_id}"


async def _find_in_flight_run(session: AsyncSession, source_id: uuid.UUID) -> Run | None:
    return (
        await session.execute(
            select(Run).where(
                Run.source_id == source_id,
                Run.status.in_(RUN_IN_FLIGHT_STATUSES),
            )
        )
    ).scalar_one_or_none()


async def _activate_run(session: AsyncSession, run: Run) -> Task:
    """Create the task for a committed pending run and mark it running."""
    task = Task(
        run_id=run.id,
        source_id=run.source_id,
        status="queued",
        idempotency_key=_task_idempotency_key(run.id, run.source_id),
        queued_at=_utcnow(),
    )
    session.add(task)
    run.status = "running"
    run.started_at = _utcnow()
    run.total_tasks = 1
    await session.commit()
    return task


def _dispatch(task_id: uuid.UUID) -> None:
    """Send the task only after task persistence commits."""
    from app.workers.celery_app import celery_app

    celery_app.send_task("app.workers.tasks_http.scrape_source_url", args=[str(task_id)])


async def create_manual_run(
    *,
    source_id: uuid.UUID,
    idempotency_key: str | None,
    attach_to_in_flight: bool,
    workspace_id: uuid.UUID | None = None,
) -> tuple[Run, Task | None, bool]:
    """Create a manual run after checking optional workspace ownership."""
    async with get_session() as session:
        source = await get_source(
            session,
            source_id,
            workspace_id=workspace_id,
        )
        if source is None:
            raise SourceNotFoundError(source_id)
        if source.status == "archived":
            raise SourceArchivedError(source_id)

        if idempotency_key:
            existing = (
                await session.execute(
                    select(Run).where(Run.client_idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.source_id != source_id:
                    raise SourceNotFoundError(source_id)
                return existing, None, False

        in_flight = await _find_in_flight_run(session, source_id)
        if in_flight is not None:
            if attach_to_in_flight:
                return in_flight, None, False
            raise RunInProgressError(source_id, in_flight.id)

        run = Run(
            source_id=source_id,
            schedule_id=None,
            status="pending",
            triggered_by="manual",
            client_idempotency_key=idempotency_key,
        )
        session.add(run)

        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            winner = await _find_in_flight_run(session, source_id)
            assert winner is not None
            if attach_to_in_flight:
                return winner, None, False
            raise RunInProgressError(source_id, winner.id) from None

        run_id = run.id

    async with get_session() as session:
        live_run = (
            await session.execute(select(Run).where(Run.id == run_id))
        ).scalar_one()
        task = await _activate_run(session, live_run)

    _dispatch(task.id)
    return live_run, task, True


async def claim_due_schedules(*, batch_size: int) -> int:
    """Claim due schedules and dispatch their runs."""
    created_run_ids: list[uuid.UUID] = []

    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT s.id AS schedule_id, s.source_id
                    FROM schedules s
                    JOIN sources src ON src.id = s.source_id
                    WHERE s.next_run_at <= now()
                      AND s.is_active
                      AND src.status = 'active'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM runs r
                          WHERE r.source_id = s.source_id
                            AND r.status IN ('pending', 'running')
                      )
                    ORDER BY s.next_run_at
                    FOR UPDATE OF s SKIP LOCKED
                    LIMIT :batch_size
                    """
                ),
                {"batch_size": batch_size},
            )
        ).all()

        for row in rows:
            await session.execute(
                text(
                    """
                    UPDATE schedules
                    SET next_run_at = now() + (interval_minutes || ' minutes')::interval,
                        last_run_at = now()
                    WHERE id = :schedule_id
                    """
                ),
                {"schedule_id": row.schedule_id},
            )

            run = Run(
                source_id=row.source_id,
                schedule_id=row.schedule_id,
                status="pending",
                triggered_by="schedule",
            )
            try:
                async with session.begin_nested():
                    session.add(run)
                    await session.flush()
            except IntegrityError:
                logger.info(
                    "schedule claim lost the no-overlap race, will retry next cycle",
                    extra={
                        "schedule_id": str(row.schedule_id),
                        "source_id": str(row.source_id),
                    },
                )
                continue

            created_run_ids.append(run.id)

        await session.commit()

    if not created_run_ids:
        return 0

    activated: list[Task] = []
    async with get_session() as session:
        for run_id in created_run_ids:
            live_run = (
                await session.execute(select(Run).where(Run.id == run_id))
            ).scalar_one()
            activated.append(await _activate_run(session, live_run))

    for task in activated:
        _dispatch(task.id)

    logger.info("scheduler claimed due schedules", extra={"claimed": len(activated)})
    return len(activated)


async def reconciliation_sweep(*, stuck_after: timedelta) -> dict[str, int]:
    """Requeue stuck tasks and heal old pending runs without a task."""
    cutoff = _utcnow() - stuck_after
    requeued_task_ids: list[uuid.UUID] = []

    async with get_session() as session:
        stuck_tasks = (
            await session.execute(
                select(Task).where(
                    Task.status.in_(("queued", "in_progress")),
                    Task.queued_at.is_not(None),
                    Task.queued_at < cutoff,
                )
            )
        ).scalars().all()

        for task in stuck_tasks:
            task.status = "queued"
            task.queued_at = _utcnow()
            requeued_task_ids.append(task.id)

        await session.commit()

    for task_id in requeued_task_ids:
        _dispatch(task_id)

    healed_tasks: list[Task] = []
    async with get_session() as session:
        orphaned_runs = (
            await session.execute(
                select(Run).where(
                    Run.status == "pending",
                    Run.created_at < cutoff,
                    ~exists().where(Task.run_id == Run.id),
                )
            )
        ).scalars().all()

        for run in orphaned_runs:
            healed_tasks.append(await _activate_run(session, run))

    for task in healed_tasks:
        _dispatch(task.id)

    if requeued_task_ids or healed_tasks:
        logger.warning(
            "reconciliation sweep found stuck work",
            extra={
                "requeued_tasks": len(requeued_task_ids),
                "healed_runs": len(healed_tasks),
            },
        )

    return {
        "requeued_tasks": len(requeued_task_ids),
        "healed_runs": len(healed_tasks),
    }


async def first_task_id_for_run(run_id: uuid.UUID) -> uuid.UUID | None:
    """Return the first task ID for a run."""
    async with get_session() as session:
        tasks = await list_run_tasks(session, run_id)
    return tasks[0].id if tasks else None


async def get_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Run | None:
    """Look up a run, optionally requiring ownership through its source."""
    query = select(Run).where(Run.id == run_id)
    if workspace_id is not None:
        query = query.join(Source, Source.id == Run.source_id).where(
            Source.workspace_id == workspace_id
        )
    return (await session.execute(query)).scalar_one_or_none()


async def list_runs(
    session: AsyncSession,
    *,
    source_id: uuid.UUID | None,
    status: str | None,
    triggered_by: str | None,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
    workspace_id: uuid.UUID | None = None,
) -> list[Run]:
    """List runs, optionally scoped through source workspace ownership."""
    query = select(Run)
    if workspace_id is not None:
        query = query.join(Source, Source.id == Run.source_id).where(
            Source.workspace_id == workspace_id
        )
    if source_id is not None:
        query = query.where(Run.source_id == source_id)
    if status is not None:
        query = query.where(Run.status == status)
    if triggered_by is not None:
        query = query.where(Run.triggered_by == triggered_by)
    if after is not None:
        after_created_at, after_id = after
        query = query.where(
            (Run.created_at < after_created_at)
            | ((Run.created_at == after_created_at) & (Run.id < after_id))
        )

    query = query.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit)
    return list((await session.execute(query)).scalars().all())


async def list_run_tasks(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> list[Task]:
    """List a run's tasks, optionally scoped through the task source."""
    query = select(Task).where(Task.run_id == run_id)
    if workspace_id is not None:
        query = query.join(Source, Source.id == Task.source_id).where(
            Source.workspace_id == workspace_id
        )
    return list((await session.execute(query)).scalars().all())


async def get_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Task | None:
    """Look up a task, optionally scoped through its source."""
    query = select(Task).where(Task.id == task_id)
    if workspace_id is not None:
        query = query.join(Source, Source.id == Task.source_id).where(
            Source.workspace_id == workspace_id
        )
    return (await session.execute(query)).scalar_one_or_none()