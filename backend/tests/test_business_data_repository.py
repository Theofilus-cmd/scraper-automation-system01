"""Integration coverage for workspace-scoped business-data repositories."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from app.db.models.business_data_repository import (
    create_business_data_run,
    create_business_data_source,
    finish_business_data_run,
    get_business_data_run,
    get_business_data_schedule,
    get_business_data_source,
    list_business_data_runs,
    list_business_data_sources,
    mark_business_data_run_running,
    upsert_business_data_schedule,
)

from app.db.models.identity import User, Workspace
from app.db.session import get_session

pytestmark = pytest.mark.integration


async def _create_workspace(*, suffix: str) -> Workspace:
    """Create a dedicated workspace for one business-data integration test."""

    async with get_session() as session:
        token = uuid.uuid4().hex
        user = User(
            email=f"business-data-{suffix}-{token}@example.test",
            password_hash="not-used-by-this-test",
            display_name=f"Business Data {suffix}",
        )
        session.add(user)
        await session.flush()

        workspace = Workspace(
            name=f"Business Data {suffix}",
            slug=f"business-data-{suffix}-{token}",
            owner_user_id=user.id,
        )
        session.add(workspace)
        await session.commit()

        return workspace


async def test_business_data_sources_are_isolated_by_workspace() -> None:
    workspace_a = await _create_workspace(suffix="a")
    workspace_b = await _create_workspace(suffix="b")

    async with get_session() as session:
        source_a = await create_business_data_source(
            session,
            workspace_id=workspace_a.id,
            name="A inventory upload",
            source_type="file_upload",
            adapter_type="csv",
            data_template="product_inventory",
            config={"delimiter": ","},
        )
        source_a_id = source_a.id
        await session.commit()

    async with get_session() as session:
        owned = await get_business_data_source(
            session,
            source_a_id,
            workspace_id=workspace_a.id,
        )
        hidden = await get_business_data_source(
            session,
            source_a_id,
            workspace_id=workspace_b.id,
        )

        sources_for_a = await list_business_data_sources(
            session,
            workspace_id=workspace_a.id,
            status=None,
            after=None,
            limit=20,
        )
        sources_for_b = await list_business_data_sources(
            session,
            workspace_id=workspace_b.id,
            status=None,
            after=None,
            limit=20,
        )

    assert owned is not None
    assert owned.id == source_a_id
    assert owned.workspace_id == workspace_a.id
    assert owned.status == "draft"
    assert owned.config == {"delimiter": ","}

    assert hidden is None
    assert [source.id for source in sources_for_a] == [source_a_id]
    assert sources_for_b == []


@pytest.mark.parametrize(
    ("interval_minutes", "accepted"),
    [
        (14, False),
        (15, True),
        (43_200, True),
        (43_201, False),
    ],
)
async def test_business_data_schedule_enforces_interval_bounds(
    interval_minutes: int,
    accepted: bool,
) -> None:
    workspace = await _create_workspace(suffix=f"interval-{interval_minutes}")

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace.id,
            name="Schedule bounds source",
            source_type="file_upload",
            adapter_type="csv",
            data_template="universal_table",
        )
        source_id = source.id
        await session.commit()

    async with get_session() as session:
        if not accepted:
            with pytest.raises(ValueError):
                await upsert_business_data_schedule(
                    session,
                    source_id,
                    workspace_id=workspace.id,
                    interval_minutes=interval_minutes,
                    is_active=True,
                )
            return

        schedule = await upsert_business_data_schedule(
            session,
            source_id,
            workspace_id=workspace.id,
            interval_minutes=interval_minutes,
            is_active=True,
        )
        assert schedule is not None
        schedule_id = schedule.id
        await session.commit()

    async with get_session() as session:
        persisted = await get_business_data_schedule(
            session,
            source_id,
            workspace_id=workspace.id,
        )

    assert persisted is not None
    assert persisted.id == schedule_id
    assert persisted.interval_minutes == interval_minutes
    assert persisted.is_active is True


async def test_business_data_schedule_is_one_to_one_and_workspace_scoped() -> None:
    workspace_a = await _create_workspace(suffix="schedule-a")
    workspace_b = await _create_workspace(suffix="schedule-b")

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace_a.id,
            name="One schedule source",
            source_type="custom_api",
            adapter_type="http_json",
            data_template="sales_orders",
        )
        source_id = source.id
        await session.commit()

    async with get_session() as session:
        first = await upsert_business_data_schedule(
            session,
            source_id,
            workspace_id=workspace_a.id,
            interval_minutes=15,
            is_active=True,
        )
        assert first is not None
        first_id = first.id
        await session.commit()

    async with get_session() as session:
        second = await upsert_business_data_schedule(
            session,
            source_id,
            workspace_id=workspace_a.id,
            interval_minutes=30,
            is_active=False,
        )
        hidden = await get_business_data_schedule(
            session,
            source_id,
            workspace_id=workspace_b.id,
        )
        blocked = await upsert_business_data_schedule(
            session,
            source_id,
            workspace_id=workspace_b.id,
            interval_minutes=15,
            is_active=True,
        )
        await session.commit()

    assert second is not None
    assert second.id == first_id
    assert second.interval_minutes == 30
    assert second.is_active is False
    assert hidden is None
    assert blocked is None

    async with get_session() as session:
        persisted = await get_business_data_schedule(
            session,
            source_id,
            workspace_id=workspace_a.id,
        )

    assert persisted is not None
    assert persisted.id == first_id
    assert persisted.interval_minutes == 30
    assert persisted.is_active is False


async def test_reactivating_business_data_schedule_moves_next_run_forward() -> None:
    workspace = await _create_workspace(suffix="schedule-reactivation")

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace.id,
            name="Reactivation source",
            source_type="webhook",
            adapter_type="incoming_json",
            data_template="universal_table",
        )
        source_id = source.id
        await session.commit()

    async with get_session() as session:
        schedule = await upsert_business_data_schedule(
            session,
            source_id,
            workspace_id=workspace.id,
            interval_minutes=15,
            is_active=False,
        )
        assert schedule is not None
        inactive_next_run_at = schedule.next_run_at
        await session.commit()

    async with get_session() as session:
        reactivated = await upsert_business_data_schedule(
            session,
            source_id,
            workspace_id=workspace.id,
            interval_minutes=30,
            is_active=True,
        )
        assert reactivated is not None
        reactivated_next_run_at = reactivated.next_run_at
        await session.commit()

    assert reactivated_next_run_at > inactive_next_run_at
async def test_business_data_run_lifecycle_is_workspace_scoped() -> None:
    workspace_a = await _create_workspace(suffix="run-lifecycle-a")
    workspace_b = await _create_workspace(suffix="run-lifecycle-b")

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace_a.id,
            name="Run lifecycle source",
            source_type="custom_api",
            adapter_type="http_json",
            data_template="sales_orders",
        )
        source_id = source.id
        await session.commit()

    async with get_session() as session:
        run = await create_business_data_run(
            session,
            source_id,
            workspace_id=workspace_a.id,
            triggered_by="manual",
            client_idempotency_key=f"run-lifecycle-{uuid.uuid4().hex}",
        )
        assert run is not None
        run_id = run.id
        await session.commit()

    assert run.status == "pending"
    assert run.triggered_by == "manual"
    assert run.started_at is None
    assert run.finished_at is None
    assert run.total_records == 0
    assert run.succeeded_records == 0
    assert run.failed_records == 0

    async with get_session() as session:
        hidden = await get_business_data_run(
            session,
            run_id,
            workspace_id=workspace_b.id,
        )
        hidden_runs = await list_business_data_runs(
            session,
            source_id,
            workspace_id=workspace_b.id,
            after=None,
            limit=20,
        )
        blocked_start = await mark_business_data_run_running(
            session,
            run_id,
            workspace_id=workspace_b.id,
        )
        blocked_finish = await finish_business_data_run(
            session,
            run_id,
            workspace_id=workspace_b.id,
            status="failed",
            total_records=1,
            succeeded_records=0,
            failed_records=1,
            error_reason="should not be visible",
        )
        await session.commit()

    assert hidden is None
    assert hidden_runs == []
    assert blocked_start is None
    assert blocked_finish is None

    async with get_session() as session:
        running = await mark_business_data_run_running(
            session,
            run_id,
            workspace_id=workspace_a.id,
        )
        assert running is not None
        started_at = running.started_at
        await session.commit()
    assert running.status == "running"
    assert started_at is not None

    async with get_session() as session:
        completed = await finish_business_data_run(
            session,
            run_id,
            workspace_id=workspace_a.id,
            status="completed",
            total_records=5,
            succeeded_records=5,
            failed_records=0,
        )
        assert completed is not None
        finished_at = completed.finished_at
        await session.commit()

    assert completed.status == "completed"
    assert completed.total_records == 5
    assert completed.succeeded_records == 5
    assert completed.failed_records == 0
    assert completed.error_reason is None
    assert completed.error_detail is None
    assert finished_at is not None
    assert finished_at >= started_at

    async with get_session() as session:
        persisted = await get_business_data_run(
            session,
            run_id,
            workspace_id=workspace_a.id,
        )

    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.started_at is not None
    assert persisted.finished_at is not None


async def test_business_data_run_rejects_invalid_transitions() -> None:
    workspace = await _create_workspace(suffix="run-invalid-transitions")

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace.id,
            name="Invalid transition source",
            source_type="webhook",
            adapter_type="incoming_json",
            data_template="universal_table",
        )
        source_id = source.id
        await session.commit()

    async with get_session() as session:
        run = await create_business_data_run(
            session,
            source_id,
            workspace_id=workspace.id,
            triggered_by="webhook",
        )
        assert run is not None
        run_id = run.id
        await session.commit()
    async with get_session() as session:
        finishing_pending = await finish_business_data_run(
            session,
            run_id,
            workspace_id=workspace.id,
            status="completed",
            total_records=0,
            succeeded_records=0,
            failed_records=0,
        )
        started = await mark_business_data_run_running(
            session,
            run_id,
            workspace_id=workspace.id,
        )
        assert started is not None
        started_at = started.started_at
        await session.commit()

    assert finishing_pending is None
    assert started.status == "running"
    assert started_at is not None

    async with get_session() as session:
        started_again = await mark_business_data_run_running(
            session,
            run_id,
            workspace_id=workspace.id,
        )
        failed = await finish_business_data_run(
            session,
            run_id,
            workspace_id=workspace.id,
            status="failed",
            total_records=3,
            succeeded_records=1,
            failed_records=2,
            error_reason="Upstream API timeout",
            error_detail={"provider": "example", "retryable": True},
        )
        assert failed is not None
        await session.commit()

    assert started_again is None
    assert failed.status == "failed"
    assert failed.error_reason == "Upstream API timeout"
    assert failed.error_detail == {"provider": "example", "retryable": True}
    assert failed.finished_at is not None

    async with get_session() as session:
        finished_again = await finish_business_data_run(
            session,
            run_id,
            workspace_id=workspace.id,
            status="completed",
            total_records=3,
            succeeded_records=3,
            failed_records=0,
        )
        restarted = await mark_business_data_run_running(
            session,
            run_id,
            workspace_id=workspace.id,
        )
        await session.commit()

    assert finished_again is None
    assert restarted is None


async def test_business_data_run_validates_trigger_and_schedule_ownership() -> None:
    workspace_a = await _create_workspace(suffix="run-schedule-a")
    workspace_b = await _create_workspace(suffix="run-schedule-b")

    async with get_session() as session:
        source_a = await create_business_data_source(
            session,
            workspace_id=workspace_a.id,
            name="Schedule-owned run source",
            source_type="file_upload",
            adapter_type="csv",
            data_template="product_inventory",
        )
        source_b = await create_business_data_source(
            session,
            workspace_id=workspace_b.id,
            name="Other schedule source",
            source_type="file_upload",
            adapter_type="csv",
            data_template="product_inventory",
        )
        source_a_id = source_a.id
        source_b_id = source_b.id
        await session.commit()

    async with get_session() as session:
        schedule_a = await upsert_business_data_schedule(
            session,
            source_a_id,
            workspace_id=workspace_a.id,
            interval_minutes=15,
            is_active=True,
        )
        schedule_b = await upsert_business_data_schedule(
            session,
            source_b_id,
            workspace_id=workspace_b.id,
            interval_minutes=15,
            is_active=True,
        )
        assert schedule_a is not None
        assert schedule_b is not None
        schedule_a_id = schedule_a.id
        schedule_b_id = schedule_b.id
        await session.commit()

    async with get_session() as session:
        with pytest.raises(ValueError):
            await create_business_data_run(
                session,
                source_a_id,
                workspace_id=workspace_a.id,
                triggered_by="not-a-trigger",
            )

        wrong_schedule = await create_business_data_run(
            session,
            source_a_id,
            workspace_id=workspace_a.id,
            triggered_by="schedule",
            business_data_schedule_id=schedule_b_id,
        )
        valid_schedule_run = await create_business_data_run(
            session,
            source_a_id,
            workspace_id=workspace_a.id,
            triggered_by="schedule",
            business_data_schedule_id=schedule_a_id,
        )
        assert valid_schedule_run is not None
        await session.commit()

    assert wrong_schedule is None
    assert valid_schedule_run.status == "pending"
    assert valid_schedule_run.business_data_schedule_id == schedule_a_id


async def test_business_data_source_allows_only_one_in_flight_run() -> None:
    workspace = await _create_workspace(suffix="single-in-flight")

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace.id,
            name="Single in-flight source",
            source_type="custom_api",
            adapter_type="http_json",
            data_template="sales_orders",
        )
        source_id = source.id
        await session.commit()

    async with get_session() as session:
        first = await create_business_data_run(
            session,
            source_id,
            workspace_id=workspace.id,
            triggered_by="manual",
        )
        assert first is not None
        await session.commit()

    async with get_session() as session:
        with pytest.raises(IntegrityError):
            await create_business_data_run(
                session,
                source_id,
                workspace_id=workspace.id,
                triggered_by="schedule",
            )

        await session.rollback()

        started = await mark_business_data_run_running(
            session,
            first.id,
            workspace_id=workspace.id,
        )
        assert started is not None
        assert started.status == "running"
        await session.commit()

    async with get_session() as session:
        with pytest.raises(IntegrityError):
            await create_business_data_run(
                session,
                source_id,
                workspace_id=workspace.id,
                triggered_by="webhook",
            )

        await session.rollback()

    async with get_session() as session:
        running = await get_business_data_run(
            session,
            first.id,
            workspace_id=workspace.id,
        )
        assert running is not None
        assert running.status == "running"

        completed = await finish_business_data_run(
            session,
            first.id,
            workspace_id=workspace.id,
            status="completed",
            total_records=0,
            succeeded_records=0,
            failed_records=0,
        )
        assert completed is not None
        await session.commit()

    async with get_session() as session:
        next_run = await create_business_data_run(
            session,
            source_id,
            workspace_id=workspace.id,
            triggered_by="schedule",
        )
        assert next_run is not None
        await session.commit()

    assert next_run.status == "pending"
