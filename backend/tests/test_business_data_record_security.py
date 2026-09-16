"""Security coverage for business-data record persistence."""

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


async def _workspace(suffix: str) -> Workspace:
    async with get_session() as session:
        token = uuid.uuid4().hex
        user = User(
            email=f"record-security-{suffix}-{token}@example.test",
            password_hash="unused",
            display_name=f"Record Security {suffix}",
        )
        session.add(user)
        await session.flush()

        workspace = Workspace(
            name=f"Record Security {suffix}",
            slug=f"record-security-{suffix}-{token}",
            owner_user_id=user.id,
        )
        session.add(workspace)
        await session.commit()
        return workspace


async def _completed_run(
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
) -> uuid.UUID:
    async with get_session() as session:
        run = await create_business_data_run(
            session,
            source_id,
            workspace_id=workspace_id,
            triggered_by="manual",
        )
        assert run is not None

        started = await mark_business_data_run_running(
            session,
            run.id,
            workspace_id=workspace_id,
        )
        assert started is not None

        finished = await finish_business_data_run(
            session,
            run.id,
            workspace_id=workspace_id,
            status="completed",
            total_records=0,
            succeeded_records=0,
            failed_records=0,
        )
        assert finished is not None
        await session.commit()
        return run.id


async def test_record_writes_require_matching_workspace_and_source_run() -> None:
    workspace_a = await _workspace("a")
    workspace_b = await _workspace("b")

    async with get_session() as session:
        source_a = await create_business_data_source(
            session,
            workspace_id=workspace_a.id,
            name="Source A",
            source_type="file_upload",
            adapter_type="csv",
            data_template="universal_table",
        )
        source_a_other = await create_business_data_source(
            session,
            workspace_id=workspace_a.id,
            name="Source A other",
            source_type="file_upload",
            adapter_type="csv",
            data_template="universal_table",
        )
        source_a_id = source_a.id
        source_a_other_id = source_a_other.id
        await session.commit()

    run_a_id = await _completed_run(workspace_a.id, source_a_id)
    run_a_other_id = await _completed_run(workspace_a.id, source_a_other_id)

    captured_at = datetime(2026, 9, 16, 11, 0, tzinfo=UTC)
    fields = {"status": "ok"}

    async with get_session() as session:
        blocked_workspace = await upsert_business_data_record(
            session,
            source_a_id,
            workspace_id=workspace_b.id,
            run_id=run_a_id,
            external_id="SEC-001",
            fields=fields,
            captured_at=captured_at,
        )
        blocked_source_run = await upsert_business_data_record(
            session,
            source_a_id,
            workspace_id=workspace_a.id,
            run_id=run_a_other_id,
            external_id="SEC-001",
            fields=fields,
            captured_at=captured_at,
        )
        valid = await upsert_business_data_record(
            session,
            source_a_id,
            workspace_id=workspace_a.id,
            run_id=run_a_id,
            external_id="SEC-001",
            fields=fields,
            captured_at=captured_at,
        )
        assert valid is not None
        record, _history = valid
        record_id = record.id
        await session.commit()

    assert blocked_workspace is None
    assert blocked_source_run is None

    async with get_session() as session:
        hidden_record = await get_business_data_record(
            session,
            record_id,
            workspace_id=workspace_b.id,
        )
        hidden_records = await list_business_data_records(
            session,
            source_a_id,
            workspace_id=workspace_b.id,
            after=None,
            limit=20,
        )
        hidden_history = await list_business_data_record_history(
            session,
            record_id,
            workspace_id=workspace_b.id,
            after=None,
            limit=20,
        )

    assert hidden_record is None
    assert hidden_records == []
    assert hidden_history == []
