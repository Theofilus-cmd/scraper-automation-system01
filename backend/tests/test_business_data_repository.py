"""Integration coverage for workspace-scoped business-data repositories."""

from __future__ import annotations

import uuid

import pytest

from app.db.models.business_data_repository import (
    create_business_data_source,
    get_business_data_schedule,
    get_business_data_source,
    list_business_data_sources,
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
