"""Integration coverage for business-data record persistence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.db.models.business_data_repository import (
    create_business_data_run,
    create_business_data_source,
    finish_business_data_run,
    get_business_data_record,
    list_business_data_record_history,
    list_business_data_records,
    mark_business_data_run_running,
    upsert_business_data_record,
)
from app.db.models.identity import User, Workspace
from app.db.session import get_session

pytestmark = pytest.mark.integration


async def _create_workspace(*, suffix: str) -> Workspace:
    """Create one isolated workspace for record-persistence tests."""

    async with get_session() as session:
        token = uuid.uuid4().hex
        user = User(
            email=f"business-record-{suffix}-{token}@example.test",
            password_hash="not-used-by-this-test",
            display_name=f"Business Record {suffix}",
        )
        session.add(user)
        await session.flush()

        workspace = Workspace(
            name=f"Business Record {suffix}",
            slug=f"business-record-{suffix}-{token}",
            owner_user_id=user.id,
        )
        session.add(workspace)
        await session.commit()
        return workspace


async def _create_completed_run(
    *,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    triggered_by: str,
) -> uuid.UUID:
    """Create, start, and complete a run so another run may be created."""

    async with get_session() as session:
        run = await create_business_data_run(
            session,
            source_id,
            workspace_id=workspace_id,
            triggered_by=triggered_by,
        )
        assert run is not None

        started = await mark_business_data_run_running(
            session,
            run.id,
            workspace_id=workspace_id,
        )
        assert started is not None

        completed = await finish_business_data_run(
            session,
            run.id,
            workspace_id=workspace_id,
            status="completed",
            total_records=0,
            succeeded_records=0,
            failed_records=0,
        )
        assert completed is not None
        await session.commit()

        return run.id


async def test_first_business_data_record_creates_current_row_and_history() -> None:
    workspace = await _create_workspace(suffix="first-record")

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace.id,
            name="Inventory source",
            source_type="file_upload",
            adapter_type="csv",
            data_template="product_inventory",
        )
        source_id = source.id
        await session.commit()

    run_id = await _create_completed_run(
        workspace_id=workspace.id,
        source_id=source_id,
        triggered_by="upload",
    )

    captured_at = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
    fields = {
        "name": "Kopi Arabica",
        "price": 75_000,
        "stock": 10,
    }

    async with get_session() as session:
        result = await upsert_business_data_record(
            session,
            source_id,
            workspace_id=workspace.id,
            run_id=run_id,
            external_id="SKU-001",
            fields=fields,
            captured_at=captured_at,
        )
        assert result is not None

        record, history = result
        assert history is not None
        record_id = record.id
        history_id = history.id
        await session.commit()

    assert record.business_data_source_id == source_id
    assert record.external_id == "SKU-001"
    assert record.fields == fields
    assert record.captured_at == captured_at
    assert record.last_run_id == run_id

    assert history.business_data_record_id == record_id
    assert history.business_data_run_id == run_id
    assert history.fields == fields
    assert history.change_summary is None
    assert history.captured_at == captured_at

    async with get_session() as session:
        persisted = await get_business_data_record(
            session,
            record_id,
            workspace_id=workspace.id,
        )
        history_rows = await list_business_data_record_history(
            session,
            record_id,
            workspace_id=workspace.id,
            after=None,
            limit=20,
        )

    assert persisted is not None
    assert persisted.id == record_id
    assert [row.id for row in history_rows] == [history_id]


async def test_business_data_record_change_appends_history_but_identical_data_does_not() -> None:
    workspace = await _create_workspace(suffix="record-changes")

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace.id,
            name="Changing inventory source",
            source_type="custom_api",
            adapter_type="http_json",
            data_template="product_inventory",
        )
        source_id = source.id
        await session.commit()

    first_run_id = await _create_completed_run(
        workspace_id=workspace.id,
        source_id=source_id,
        triggered_by="manual",
    )

    first_captured_at = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
    first_fields = {
        "name": "Kopi Arabica",
        "price": 75_000,
        "stock": 10,
    }

    async with get_session() as session:
        first_result = await upsert_business_data_record(
            session,
            source_id,
            workspace_id=workspace.id,
            run_id=first_run_id,
            external_id="SKU-CHANGE-001",
            fields=first_fields,
            captured_at=first_captured_at,
        )
        assert first_result is not None
        record, first_history = first_result
        assert first_history is not None

        record_id = record.id
        first_history_id = first_history.id
        first_updated_at = record.updated_at
        await session.commit()

    second_run_id = await _create_completed_run(
        workspace_id=workspace.id,
        source_id=source_id,
        triggered_by="upload",
    )

    changed_captured_at = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
    changed_fields = {
        "name": "Kopi Arabica",
        "price": 75_000,
        "stock": 7,
    }

    async with get_session() as session:
        changed_result = await upsert_business_data_record(
            session,
            source_id,
            workspace_id=workspace.id,
            run_id=second_run_id,
            external_id="SKU-CHANGE-001",
            fields=changed_fields,
            captured_at=changed_captured_at,
        )
        assert changed_result is not None
        changed_record, changed_history = changed_result
        assert changed_history is not None

        changed_history_id = changed_history.id
        changed_updated_at = changed_record.updated_at
        await session.commit()

    assert changed_record.id == record_id
    assert changed_record.fields == changed_fields
    assert changed_record.captured_at == changed_captured_at
    assert changed_record.last_run_id == second_run_id
    assert changed_updated_at >= first_updated_at

    assert changed_history.business_data_record_id == record_id
    assert changed_history.business_data_run_id == second_run_id
    assert changed_history.fields == changed_fields
    assert changed_history.change_summary == {
        "stock": {
            "before": 10,
            "after": 7,
        }
    }
    assert changed_history.captured_at == changed_captured_at

    third_run_id = await _create_completed_run(
        workspace_id=workspace.id,
        source_id=source_id,
        triggered_by="webhook",
    )

    unchanged_captured_at = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)

    async with get_session() as session:
        unchanged_result = await upsert_business_data_record(
            session,
            source_id,
            workspace_id=workspace.id,
            run_id=third_run_id,
            external_id="SKU-CHANGE-001",
            fields=changed_fields,
            captured_at=unchanged_captured_at,
        )
        assert unchanged_result is not None
        unchanged_record, unchanged_history = unchanged_result
        unchanged_updated_at = unchanged_record.updated_at
        await session.commit()

    assert unchanged_record.id == record_id
    assert unchanged_history is None
    assert unchanged_record.fields == changed_fields
    assert unchanged_record.captured_at == unchanged_captured_at
    assert unchanged_record.last_run_id == third_run_id
    assert unchanged_updated_at >= changed_updated_at

    async with get_session() as session:
        current_records = await list_business_data_records(
            session,
            source_id,
            workspace_id=workspace.id,
            after=None,
            limit=20,
        )
        history_rows = await list_business_data_record_history(
            session,
            record_id,
            workspace_id=workspace.id,
            after=None,
            limit=20,
        )

    assert [row.id for row in current_records] == [record_id]
    assert [row.id for row in history_rows] == [
        changed_history_id,
        first_history_id,
    ]
