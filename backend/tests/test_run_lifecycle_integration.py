"""doc 18 §8.2: run-lifecycle integration tests against a real Postgres
(and a real Redis -- create_manual_run()/claim_due_schedules()/
reconciliation_sweep() all dispatch to Celery on success, doc 18 §4.1's
phase C; nothing here needs a worker to actually *consume* those tasks,
only Postgres's own run/task rows are asserted on) -- the no-overlap
invariant (§4.4), scheduler claim correctness (§4.1), and the
reconciliation sweep's two self-healing cases (§1.1, extended per
runs_repository.py's own module docstring).

    docker compose exec api pytest -m integration
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models.lifecycle import Run, Schedule, Task
from app.db.models.runs_repository import (
    claim_due_schedules,
    create_manual_run,
    reconciliation_sweep,
)
from app.db.models.sources_repository import upsert_schedule
from app.db.session import get_session
from app.domain.errors import RunInProgressError
from tests.factories import create_test_run, create_test_source, create_test_task

pytestmark = pytest.mark.integration


async def _force_schedule_due(source_id: uuid.UUID) -> Schedule:
    """upsert_schedule() always sets next_run_at in the future (now() +
    interval_minutes) -- correct for real scheduling, useless for testing
    "this schedule is due right now" without waiting. Backdates it
    directly afterward, the same way real time passing eventually would.
    """
    async with get_session() as session:
        schedule = await upsert_schedule(session, source_id, interval_minutes=15, is_active=True)
        schedule.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
        return schedule


async def test_no_overlap_single_process_second_attempt_collides() -> None:
    """doc 18 §4.4/§8.2: a second attempt to create a run for a source
    that already has one in flight is rejected -- proving the invariant
    itself, not just design argument. attach_to_in_flight=False is the
    new endpoint's own mode (doc 18 §6.2): a collision must raise, never
    silently attach.
    """
    source = await create_test_source()

    first_run, first_task, first_created = await create_manual_run(
        source_id=source.id, idempotency_key=None, attach_to_in_flight=False
    )
    assert first_created is True
    assert first_task is not None
    assert first_run.status == "running"

    with pytest.raises(RunInProgressError) as exc_info:
        await create_manual_run(
            source_id=source.id, idempotency_key=None, attach_to_in_flight=False
        )
    assert exc_info.value.existing_run_id == first_run.id


async def test_attach_to_in_flight_returns_the_existing_run_instead_of_erroring() -> None:
    """doc 18 §6.6: the legacy alias's own mode -- attach_to_in_flight=True
    never raises on a collision, it transparently returns whatever run is
    already in flight.
    """
    source = await create_test_source()
    first_run, _first_task, first_created = await create_manual_run(
        source_id=source.id, idempotency_key=None, attach_to_in_flight=True
    )
    assert first_created is True

    second_run, second_task, second_created = await create_manual_run(
        source_id=source.id, idempotency_key=None, attach_to_in_flight=True
    )

    assert second_created is False
    assert second_task is None
    assert second_run.id == first_run.id


async def test_idempotency_key_replay_returns_the_original_run() -> None:
    """doc 18 §4.4: a replayed call carrying a matching idempotency key
    returns the original run directly, before the in-flight-run check is
    even consulted -- so this must succeed even though the first run is
    itself still in flight (an ordinary second call with no key would
    raise RunInProgressError instead, per the test above).
    """
    source = await create_test_source()
    key = f"test-idem-{uuid.uuid4().hex}"

    first_run, first_task, first_created = await create_manual_run(
        source_id=source.id, idempotency_key=key, attach_to_in_flight=False
    )
    assert first_created is True
    assert first_task is not None

    replayed_run, replayed_task, replayed_created = await create_manual_run(
        source_id=source.id, idempotency_key=key, attach_to_in_flight=False
    )

    assert replayed_created is False
    assert replayed_task is None
    assert replayed_run.id == first_run.id


async def test_claim_due_schedules_creates_and_activates_a_run() -> None:
    source = await create_test_source()
    schedule = await _force_schedule_due(source.id)

    claimed = await claim_due_schedules(batch_size=20)

    assert claimed >= 1
    async with get_session() as session:
        run = (await session.execute(select(Run).where(Run.source_id == source.id))).scalar_one()
        task = (await session.execute(select(Task).where(Task.run_id == run.id))).scalar_one()
        refreshed_schedule = (
            await session.execute(select(Schedule).where(Schedule.id == schedule.id))
        ).scalar_one()

    assert run.status == "running"
    assert run.triggered_by == "schedule"
    assert run.schedule_id == schedule.id
    assert task.status == "queued"
    # The schedule is no longer due for this same cycle (doc 18 §4.1).
    assert refreshed_schedule.next_run_at > datetime.now(UTC)


async def test_claim_due_schedules_skips_a_source_with_an_in_flight_run() -> None:
    """doc 18 §4.1's WHERE-clause fast path: a source already blocked by
    an in-flight run is excluded from the claim query entirely."""
    source = await create_test_source()
    await create_test_run(source.id, status="running")  # blocks this source
    await _force_schedule_due(source.id)

    async with get_session() as session:
        before = (
            await session.execute(select(Run).where(Run.source_id == source.id))
        ).scalars().all()

    await claim_due_schedules(batch_size=20)

    async with get_session() as session:
        after = (
            await session.execute(select(Run).where(Run.source_id == source.id))
        ).scalars().all()
    # No second run was created for this source -- the pre-existing
    # in-flight run is still the only one.
    assert len(after) == len(before) == 1


async def test_reconciliation_sweep_requeues_a_stuck_task() -> None:
    source = await create_test_source()
    run = await create_test_run(source.id, status="running")
    stuck_queued_at = datetime.now(UTC) - timedelta(minutes=30)
    task = await create_test_task(run.id, source.id, status="queued", queued_at=stuck_queued_at)

    result = await reconciliation_sweep(stuck_after=timedelta(minutes=10))

    assert result["requeued_tasks"] >= 1
    async with get_session() as session:
        refreshed = (await session.execute(select(Task).where(Task.id == task.id))).scalar_one()
    assert refreshed.status == "queued"
    assert refreshed.queued_at is not None
    assert refreshed.queued_at > stuck_queued_at  # re-stamped, not left stale


async def test_reconciliation_sweep_heals_an_orphaned_pending_run() -> None:
    """runs_repository.py's own documented extension beyond doc 07 §5's
    literal query: a run that committed pending (phase A) but crashed
    before phase B ever created its task -- invisible to the stuck-task
    query above, since there is no task row yet at all.
    """
    source = await create_test_source()
    old_pending_run = await create_test_run(source.id, status="pending")
    async with get_session() as session:
        run_row = (
            await session.execute(select(Run).where(Run.id == old_pending_run.id))
        ).scalar_one()
        run_row.created_at = datetime.now(UTC) - timedelta(minutes=30)
        await session.commit()

    result = await reconciliation_sweep(stuck_after=timedelta(minutes=10))

    assert result["healed_runs"] >= 1
    async with get_session() as session:
        refreshed_run = (
            await session.execute(select(Run).where(Run.id == old_pending_run.id))
        ).scalar_one()
        healed_task = (
            await session.execute(select(Task).where(Task.run_id == old_pending_run.id))
        ).scalar_one_or_none()
    assert refreshed_run.status == "running"
    assert healed_task is not None
    assert healed_task.status == "queued"


async def test_reconciliation_sweep_leaves_a_fresh_pending_run_alone() -> None:
    """A run that just committed phase A moments ago (well within
    stuck_after) must not be "healed" prematurely -- phase B may
    legitimately still be about to run in the very next line of whatever
    created it.
    """
    source = await create_test_source()
    fresh_pending_run = await create_test_run(source.id, status="pending")

    await reconciliation_sweep(stuck_after=timedelta(minutes=10))

    async with get_session() as session:
        refreshed = (
            await session.execute(select(Run).where(Run.id == fresh_pending_run.id))
        ).scalar_one()
    assert refreshed.status == "pending"  # untouched
