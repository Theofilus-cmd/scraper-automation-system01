"""Integration tests for durable run/task lifecycle behavior."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.db.models.lifecycle import Run, Task
from app.db.models.runs_repository import (
    claim_due_schedules,
    create_manual_run,
    first_task_id_for_run,
    get_run,
    get_task,
    list_run_tasks,
    reconciliation_sweep,
)
from app.db.models.sources_repository import upsert_schedule
from app.db.session import get_session
from app.domain.errors import RunInProgressError
from tests.factories import (
    create_test_run,
    create_test_source,
    create_test_task,
    finalize_run_and_task,
)

pytestmark = pytest.mark.integration


async def test_no_overlap_single_process_second_attempt_collides() -> None:
    source = await create_test_source()

    with patch("app.db.models.runs_repository._dispatch"):
        first_run, first_task, created = await create_manual_run(
            source_id=source.id,
            idempotency_key=None,
            attach_to_in_flight=False,
        )

        assert created is True
        assert first_task is not None
        assert first_run.status == "running"

        with pytest.raises(RunInProgressError):
            await create_manual_run(
                source_id=source.id,
                idempotency_key=None,
                attach_to_in_flight=False,
            )


async def test_attach_to_in_flight_returns_the_existing_run_instead_of_erroring() -> None:
    source = await create_test_source()

    with patch("app.db.models.runs_repository._dispatch"):
        original_run, original_task, created = await create_manual_run(
            source_id=source.id,
            idempotency_key=None,
            attach_to_in_flight=False,
        )

        attached_run, attached_task, attached_created = await create_manual_run(
            source_id=source.id,
            idempotency_key=None,
            attach_to_in_flight=True,
        )

    assert created is True
    assert original_task is not None
    assert attached_created is False
    assert attached_task is None
    assert attached_run.id == original_run.id


async def test_idempotency_key_replay_returns_the_original_run() -> None:
    source = await create_test_source()
    key = f"test-key-{uuid.uuid4().hex}"

    with patch("app.db.models.runs_repository._dispatch"):
        original_run, original_task, created = await create_manual_run(
            source_id=source.id,
            idempotency_key=key,
            attach_to_in_flight=False,
        )

        replay_run, replay_task, replay_created = await create_manual_run(
            source_id=source.id,
            idempotency_key=key,
            attach_to_in_flight=False,
        )

    assert created is True
    assert original_task is not None
    assert replay_created is False
    assert replay_task is None
    assert replay_run.id == original_run.id


async def test_claim_due_schedules_creates_and_activates_a_run() -> None:
    source = await create_test_source()

    async with get_session() as session:
        schedule = await upsert_schedule(
            session,
            source.id,
            interval_minutes=30,
            is_active=True,
        )
        schedule.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    with patch("app.db.models.runs_repository._dispatch"):
        claimed = await claim_due_schedules(batch_size=10)

    assert claimed >= 1

    async with get_session() as session:
        run = (
            await session.execute(
                select(Run)
                .where(Run.source_id == source.id)
                .order_by(Run.created_at.desc())
            )
        ).scalars().first()

        assert run is not None
        assert run.status == "running"

        task = (
            await session.execute(
                select(Task).where(Task.run_id == run.id)
            )
        ).scalar_one_or_none()

    assert task is not None
    assert task.status == "queued"
    assert task.source_id == source.id


async def test_claim_due_schedules_skips_a_source_with_an_in_flight_run() -> None:
    source = await create_test_source()
    await create_test_run(source.id, status="running")

    async with get_session() as session:
        schedule = await upsert_schedule(
            session,
            source.id,
            interval_minutes=30,
            is_active=True,
        )
        schedule.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    with patch("app.db.models.runs_repository._dispatch"):
        claimed = await claim_due_schedules(batch_size=10)

    assert claimed == 0


async def test_reconciliation_sweep_requeues_a_stuck_task() -> None:
    source = await create_test_source()
    run = await create_test_run(source.id, status="running")
    stuck_queued_at = datetime.now(UTC) - timedelta(minutes=30)
    task = await create_test_task(
        run.id,
        source.id,
        status="queued",
        queued_at=stuck_queued_at,
    )

    with patch("app.db.models.runs_repository._dispatch") as dispatch:
        result = await reconciliation_sweep(stuck_after=timedelta(minutes=10))

    assert result["requeued_tasks"] >= 1
    dispatch.assert_any_call(task.id)

    async with get_session() as session:
        refreshed = (
            await session.execute(select(Task).where(Task.id == task.id))
        ).scalar_one()

    assert refreshed.status == "queued"
    assert refreshed.queued_at is not None
    assert refreshed.queued_at > stuck_queued_at


async def test_reconciliation_sweep_heals_an_orphaned_pending_run() -> None:
    source = await create_test_source()
    run = await create_test_run(source.id, status="pending")
    old_created_at = datetime.now(UTC) - timedelta(minutes=30)

    async with get_session() as session:
        live_run = (
            await session.execute(select(Run).where(Run.id == run.id))
        ).scalar_one()
        live_run.created_at = old_created_at
        await session.commit()

    with patch("app.db.models.runs_repository._dispatch") as dispatch:
        result = await reconciliation_sweep(stuck_after=timedelta(minutes=10))

    assert result["healed_runs"] >= 1
    dispatch.assert_called()

    async with get_session() as session:
        refreshed_run = (
            await session.execute(select(Run).where(Run.id == run.id))
        ).scalar_one()
        refreshed_task = (
            await session.execute(select(Task).where(Task.run_id == run.id))
        ).scalar_one()

    assert refreshed_run.status == "running"
    assert refreshed_task.status == "queued"


async def test_reconciliation_sweep_leaves_a_fresh_pending_run_alone() -> None:
    source = await create_test_source()
    run = await create_test_run(source.id, status="pending")

    with patch("app.db.models.runs_repository._dispatch") as dispatch:
        result = await reconciliation_sweep(stuck_after=timedelta(minutes=10))

    assert result["healed_runs"] == 0
    dispatch.assert_not_called()

    async with get_session() as session:
        refreshed_run = (
            await session.execute(select(Run).where(Run.id == run.id))
        ).scalar_one()

    assert refreshed_run.status == "pending"


async def test_get_run_returns_none_for_an_unknown_id() -> None:
    async with get_session() as session:
        run = await get_run(session, uuid.uuid4())

    assert run is None


async def test_get_task_returns_none_for_an_unknown_id() -> None:
    async with get_session() as session:
        task = await get_task(session, uuid.uuid4())

    assert task is None


async def test_first_task_id_for_run_returns_the_first_task() -> None:
    source = await create_test_source()
    run = await create_test_run(source.id, status="completed")
    first_task = await create_test_task(run.id, source.id, status="succeeded")
    await create_test_task(run.id, source.id, status="succeeded")

    task_id = await first_task_id_for_run(run.id)

    assert task_id == first_task.id


async def test_list_run_tasks_returns_all_tasks_for_a_run() -> None:
    source = await create_test_source()
    run = await create_test_run(source.id, status="completed")
    first_task = await create_test_task(run.id, source.id, status="succeeded")
    second_task = await create_test_task(run.id, source.id, status="succeeded")

    async with get_session() as session:
        tasks = await list_run_tasks(session, run.id)

    assert {task.id for task in tasks} == {first_task.id, second_task.id}


async def test_terminal_run_allows_a_new_manual_run() -> None:
    source = await create_test_source()
    prior_run = await create_test_run(source.id, status="running")
    prior_task = await create_test_task(prior_run.id, source.id, status="queued")
    await finalize_run_and_task(prior_run.id, prior_task.id)

    with patch("app.db.models.runs_repository._dispatch"):
        new_run, task, created = await create_manual_run(
            source_id=source.id,
            idempotency_key=None,
            attach_to_in_flight=False,
        )

    assert created is True
    assert task is not None
    assert new_run.id != prior_run.id