"""Integration coverage for business-data ingestion orchestration."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.db.models.business_data_repository import (
    create_business_data_run,
    create_business_data_source,
    get_business_data_run,
    list_business_data_record_history,
    list_business_data_records,
)
from app.db.models.identity import User, Workspace
from app.db.session import get_session
from app.domain.business_data_ingestion import (
    BusinessDataIngestionItem,
    ingest_business_data_records,
)

pytestmark = pytest.mark.integration


async def _create_workspace() -> Workspace:
    async with get_session() as session:
        token = uuid.uuid4().hex
        user = User(
            email=f"ingestion-{token}@example.test",
            password_hash="not-used-by-this-test",
            display_name="Business Data Ingestion",
        )
        session.add(user)
        await session.flush()

        workspace = Workspace(
            name="Business Data Ingestion",
            slug=f"business-data-ingestion-{token}",
            owner_user_id=user.id,
        )
        session.add(workspace)
        await session.commit()
        return workspace


async def test_ingestion_completes_run_and_persists_all_valid_items() -> None:
    workspace = await _create_workspace()

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace.id,
            name="Ingestion inventory",
            source_type="file_upload",
            adapter_type="csv",
            data_template="product_inventory",
        )
        source_id = source.id

        run = await create_business_data_run(
            session,
            source_id,
            workspace_id=workspace.id,
            triggered_by="upload",
        )
        assert run is not None
        run_id = run.id
        await session.commit()

    result = await ingest_business_data_records(
        run_id,
        workspace_id=workspace.id,
        items=[
            BusinessDataIngestionItem(
                external_id="SKU-001",
                fields={"name": "Kopi Arabica", "stock": 10},
                captured_at=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
            ),
            BusinessDataIngestionItem(
                external_id="SKU-002",
                fields={"name": "Kopi Robusta", "stock": 5},
                captured_at=datetime(2026, 9, 17, 10, 1, tzinfo=UTC),
            ),
        ],
    )

    assert result.run_id == run_id
    assert result.status == "completed"
    assert result.total_records == 2
    assert result.succeeded_records == 2
    assert result.failed_records == 0
    assert result.errors == ()

    async with get_session() as session:
        persisted_run = await get_business_data_run(
            session,
            run_id,
            workspace_id=workspace.id,
        )
        records = await list_business_data_records(
            session,
            source_id,
            workspace_id=workspace.id,
            after=None,
            limit=20,
        )

    assert persisted_run is not None
    assert persisted_run.status == "completed"
    assert persisted_run.total_records == 2
    assert persisted_run.succeeded_records == 2
    assert persisted_run.failed_records == 0
    assert persisted_run.started_at is not None
    assert persisted_run.finished_at is not None

    assert {record.external_id for record in records} == {"SKU-001", "SKU-002"}

    async with get_session() as session:
        histories = []
        for record in records:
            histories.extend(
                await list_business_data_record_history(
                    session,
                    record.id,
                    workspace_id=workspace.id,
                    after=None,
                    limit=20,
                )
            )

    assert len(histories) == 2
    assert {history.business_data_run_id for history in histories} == {run_id}
    assert all(history.change_summary is None for history in histories)


async def test_ingestion_completes_with_errors_for_invalid_items() -> None:
    workspace = await _create_workspace()

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace.id,
            name="Partial ingestion inventory",
            source_type="file_upload",
            adapter_type="csv",
            data_template="product_inventory",
        )
        source_id = source.id

        run = await create_business_data_run(
            session,
            source_id,
            workspace_id=workspace.id,
            triggered_by="upload",
        )
        assert run is not None
        run_id = run.id
        await session.commit()

    result = await ingest_business_data_records(
        run_id,
        workspace_id=workspace.id,
        items=[
            BusinessDataIngestionItem(
                external_id="SKU-VALID",
                fields={"name": "Teh Hijau", "stock": 12},
                captured_at=datetime(2026, 9, 17, 11, 0, tzinfo=UTC),
            ),
            BusinessDataIngestionItem(
                external_id="   ",
                fields={"name": "Invalid row", "stock": 0},
                captured_at=datetime(2026, 9, 17, 11, 1, tzinfo=UTC),
            ),
        ],
    )

    assert result.run_id == run_id
    assert result.status == "completed_with_errors"
    assert result.total_records == 2
    assert result.succeeded_records == 1
    assert result.failed_records == 1
    assert len(result.errors) == 1
    assert result.errors[0].external_id == "   "
    assert result.errors[0].reason == "external_id must be a non-empty string"

    async with get_session() as session:
        persisted_run = await get_business_data_run(
            session,
            run_id,
            workspace_id=workspace.id,
        )
        records = await list_business_data_records(
            session,
            source_id,
            workspace_id=workspace.id,
            after=None,
            limit=20,
        )

    assert persisted_run is not None
    assert persisted_run.status == "completed_with_errors"
    assert persisted_run.total_records == 2
    assert persisted_run.succeeded_records == 1
    assert persisted_run.failed_records == 1
    assert persisted_run.error_reason == "one or more records failed to persist"
    assert persisted_run.error_detail == {
        "item_errors": [
            {
                "external_id": "   ",
                "reason": "external_id must be a non-empty string",
            }
        ]
    }

    assert [record.external_id for record in records] == ["SKU-VALID"]

    async with get_session() as session:
        history_rows = await list_business_data_record_history(
            session,
            records[0].id,
            workspace_id=workspace.id,
            after=None,
            limit=20,
        )

    assert len(history_rows) == 1
    assert history_rows[0].business_data_run_id == run_id
    assert history_rows[0].change_summary is None


async def test_ingestion_marks_run_failed_after_unexpected_persistence_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _create_workspace()

    async with get_session() as session:
        source = await create_business_data_source(
            session,
            workspace_id=workspace.id,
            name="Fatal ingestion inventory",
            source_type="file_upload",
            adapter_type="csv",
            data_template="product_inventory",
        )
        source_id = source.id

        run = await create_business_data_run(
            session,
            source_id,
            workspace_id=workspace.id,
            triggered_by="upload",
        )
        assert run is not None
        run_id = run.id
        await session.commit()

    async def failing_upsert(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated persistence outage")

    monkeypatch.setattr(
        "app.domain.business_data_ingestion.upsert_business_data_record",
        failing_upsert,
    )

    result = await ingest_business_data_records(
        run_id,
        workspace_id=workspace.id,
        items=[
            BusinessDataIngestionItem(
                external_id="SKU-FATAL",
                fields={"name": "Gula Aren", "stock": 7},
                captured_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
            )
        ],
    )

    assert result.run_id == run_id
    assert result.status == "failed"
    assert result.total_records == 1
    assert result.succeeded_records == 0
    assert result.failed_records == 1
    assert len(result.errors) == 1
    assert result.errors[0].external_id == ""
    assert result.errors[0].reason == "simulated persistence outage"

    async with get_session() as session:
        persisted_run = await get_business_data_run(
            session,
            run_id,
            workspace_id=workspace.id,
        )
        records = await list_business_data_records(
            session,
            source_id,
            workspace_id=workspace.id,
            after=None,
            limit=20,
        )

    assert persisted_run is not None
    assert persisted_run.status == "failed"
    assert persisted_run.total_records == 1
    assert persisted_run.succeeded_records == 0
    assert persisted_run.failed_records == 1
    assert persisted_run.error_reason == "simulated persistence outage"
    assert persisted_run.error_detail == {
        "message": "simulated persistence outage",
    }
    assert persisted_run.started_at is not None
    assert persisted_run.finished_at is not None
    assert records == []
