"""Run/task creation, claiming, and query repository functions (doc 18
§3.2, §4, §5.4, §6.2, §6.6, §7.5's reconciliation extension).

Two-phase run creation (doc 18 §4.1's literal pseudocode, reused for every
trigger path -- scheduler, manual, legacy): phase A inserts the `runs` row
as `pending` and commits by itself; only after that commit succeeds does
phase B create the one `tasks` row and flip the run to `running`; only
after phase B's own commit does phase C -- the actual Celery dispatch --
happen. This mirrors §4.1's scheduler pseudocode exactly (a run that fails
phase A's insert because of a genuine no-overlap collision never gets a
task or a dispatch at all), and lets every trigger path share one
implementation of the risky part instead of three independent copies of it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.lifecycle import RUN_IN_FLIGHT_STATUSES, Run, Task
from app.db.models.sources_repository import get_source
from app.db.session import get_session
from app.domain.errors import RunInProgressError, SourceArchivedError, SourceNotFoundError

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _task_idempotency_key(run_id: uuid.UUID, source_id: uuid.UUID) -> str:
    # doc 07 §4's `hash(run_id, target_id)` shape, reused directly (C4:
    # target_id -> source_id). run_id already makes this unique per
    # attempt-series (a run has exactly one task today, §2.3's cardinality
    # note), so the plain composite string is sufficient -- no separate
    # hash function needed.
    return f"{run_id}:{source_id}"


async def _find_in_flight_run(session: AsyncSession, source_id: uuid.UUID) -> Run | None:
    result: Run | None = (
        await session.execute(
            select(Run).where(Run.source_id == source_id, Run.status.in_(RUN_IN_FLIGHT_STATUSES))
        )
    ).scalar_one_or_none()
    return result


async def _activate_run(session: AsyncSession, run: Run) -> Task:
    """Phase B (doc 18 §4.1): create the one `tasks` row, flip the run to
    `running`. Must only ever be called after phase A's own commit
    succeeded (the run row already durably exists as `pending`)."""
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
    # `task.id` is already populated post-commit without an explicit
    # refresh: Postgres's asyncpg dialect uses implicit RETURNING for a
    # server-generated PK on a single-row ORM insert (SQLAlchemy 2.0
    # default), and this session's factory sets expire_on_commit=False
    # (app/db/session.py), so no attribute is invalidated by the commit
    # above either.
    return task


def _dispatch(task_id: uuid.UUID) -> None:
    """Phase C: Celery dispatch, always strictly after phase B's commit
    (doc 18 §4.1). Imported lazily to keep `db/models/` -> `workers/` a
    call-time-only dependency, matching doc 04 §2's module-boundary intent
    (workers/ imports db/models/, not the other way around at import time).
    """
    from app.workers.celery_app import celery_app

    celery_app.send_task("app.workers.tasks_http.scrape_source_url", args=[str(task_id)])


async def create_manual_run(
    *, source_id: uuid.UUID, idempotency_key: str | None, attach_to_in_flight: bool
) -> tuple[Run, Task | None, bool]:
    """doc 18 §4.4/§6.2/§6.6: create a new manually-triggered run for
    `source_id`, or -- when `attach_to_in_flight` is True (the legacy
    alias's behavior, §6.6) -- transparently return whatever run is already
    in-flight instead of raising. Returns `(run, task_or_none, created)`;
    `task` is populated only when this call itself created+activated the
    run.

    Raises `SourceNotFoundError`, `SourceArchivedError`, or (only when
    `attach_to_in_flight` is False) `RunInProgressError`.
    """
    async with get_session() as session:
        source = await get_source(session, source_id)
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
            # The pre-check above narrowed the race but didn't close it
            # (doc 18 §4.4) -- uq_runs_one_in_flight_per_source is the real
            # backstop. Whoever lost the race reloads whichever run now
            # holds the slot.
            await session.rollback()
            winner = await _find_in_flight_run(session, source_id)
            assert winner is not None  # the violation proves one exists
            if attach_to_in_flight:
                return winner, None, False
            raise RunInProgressError(source_id, winner.id) from None
        run_id = run.id

    async with get_session() as session:
        live_run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        task = await _activate_run(session, live_run)

    _dispatch(task.id)
    return live_run, task, True


async def claim_due_schedules(*, batch_size: int) -> int:
    """doc 18 §4.1: the scheduler's atomic claim-and-dispatch. Returns the
    number of runs newly created and dispatched this cycle.
    """
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
                          SELECT 1 FROM runs r
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
            session.add(run)
            try:
                # A per-row SAVEPOINT: a lost race on ONE row must not
                # abort the whole batch, and must not release the FOR
                # UPDATE locks this transaction still holds on the OTHER
                # claimed schedule rows (a plain per-row commit would do
                # exactly that -- releasing every lock this transaction
                # holds, not just the failing row's).
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError:
                # Realistic cause: a manual/legacy trigger raced this exact
                # claim cycle for the same source (two schedules for one
                # source is impossible -- schedules.source_id is UNIQUE).
                # Per §4.1's comment: skip, the schedule stays due and is
                # reconsidered next poll cycle -- never fail the batch.
                logger.info(
                    "schedule claim lost the no-overlap race, will retry next cycle",
                    extra={"schedule_id": str(row.schedule_id), "source_id": str(row.source_id)},
                )
                continue
            created_run_ids.append(run.id)

        await session.commit()

    if not created_run_ids:
        return 0

    activated: list[Task] = []
    async with get_session() as session:
        for run_id in created_run_ids:
            live_run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            activated.append(await _activate_run(session, live_run))

    for task in activated:
        _dispatch(task.id)

    logger.info("scheduler claimed due schedules", extra={"claimed": len(activated)})
    return len(activated)


async def reconciliation_sweep(*, stuck_after: timedelta) -> dict[str, int]:
    """doc 07 §5, reused directly (doc 18 §1.1) for tasks stuck in
    `queued`/`in_progress` past `stuck_after` -- re-enqueued with their
    existing `idempotency_key`, safe by construction (doc 07 §4).

    Extended, beyond doc 07 §5's literal query, to also heal a `run` that
    committed as `pending` (doc 18 §4.1's phase A) but never reached phase
    B (its one `tasks` row created, which is what flips the run to
    `running`) -- a crash in exactly that window would otherwise leave the
    run stuck forever, since doc 07 §5 only ever looks at existing `tasks`
    rows and this run has none yet. Self-heals the same way: create the
    missing task, dispatch it, like any other stuck-task recovery. Flagged
    here and in the delivery report as a small, deliberate extension beyond
    doc 18's literal text, not a silent deviation from it.
    """
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
            extra={"requeued_tasks": len(requeued_task_ids), "healed_runs": len(healed_tasks)},
        )
    return {"requeued_tasks": len(requeued_task_ids), "healed_runs": len(healed_tasks)}


async def first_task_id_for_run(run_id: uuid.UUID) -> uuid.UUID | None:
    """Added alongside the API layer (commit 4): resolves a run's one task
    id without the caller needing its own open session -- used wherever a
    caller has a `Run` but not (yet) its `Task`: an idempotency-key replay
    (§4.4) or the legacy alias attaching to an already-in-flight run
    (§6.6), both in app/api/v1/. Returns `None` only in the narrow window
    where phase A (§4.1) has committed but phase B hasn't yet -- callers
    that can wait poll again rather than treating that as an error.
    """
    async with get_session() as session:
        tasks = await list_run_tasks(session, run_id)
    return tasks[0].id if tasks else None


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    result: Run | None = (
        await session.execute(select(Run).where(Run.id == run_id))
    ).scalar_one_or_none()
    return result


async def list_runs(
    session: AsyncSession,
    *,
    source_id: uuid.UUID | None,
    status: str | None,
    triggered_by: str | None,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[Run]:
    query = select(Run).order_by(Run.created_at.desc(), Run.id.desc()).limit(limit)
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
    return list((await session.execute(query)).scalars().all())


async def list_run_tasks(session: AsyncSession, run_id: uuid.UUID) -> list[Task]:
    return list((await session.execute(select(Task).where(Task.run_id == run_id))).scalars().all())


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    result: Task | None = (
        await session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one_or_none()
    return result
