"""HTTP integration coverage for business-data source ingestion."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def _create_source(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/v1/business-data/sources",
        json={
            "name": f"Business data API test {uuid.uuid4().hex}",
            "source_type": "file_upload",
            "adapter_type": "csv",
            "data_template": "product_inventory",
            "config": {"delimiter": ","},
        },
    )

    assert response.status_code == 201
    return response.json()


def test_create_source_ingest_items_and_list_runs(client: TestClient) -> None:
    source = _create_source(client)
    source_id = str(source["id"])

    assert set(source) == {
        "id",
        "name",
        "source_type",
        "adapter_type",
        "status",
        "data_template",
        "config",
        "created_at",
        "updated_at",
    }
    assert source["source_type"] == "file_upload"
    assert source["adapter_type"] == "csv"
    assert source["data_template"] == "product_inventory"
    assert source["status"] == "draft"
    assert source["config"] == {"delimiter": ","}

    ingest_response = client.post(
        f"/api/v1/business-data/sources/{source_id}/ingest",
        json={
            "triggered_by": "upload",
            "items": [
                {
                    "external_id": "SKU-API-001",
                    "fields": {"name": "Kopi Arabica", "stock": 10},
                    "captured_at": "2026-09-18T10:00:00+07:00",
                },
                {
                    "external_id": "SKU-API-002",
                    "fields": {"name": "Kopi Robusta", "stock": 5},
                    "captured_at": "2026-09-18T10:01:00+07:00",
                },
            ],
        },
    )

    assert ingest_response.status_code == 200
    ingest_body = ingest_response.json()
    assert set(ingest_body) == {
        "run_id",
        "status",
        "total_records",
        "succeeded_records",
        "failed_records",
        "errors",
    }
    assert ingest_body["status"] == "completed"
    assert ingest_body["total_records"] == 2
    assert ingest_body["succeeded_records"] == 2
    assert ingest_body["failed_records"] == 0
    assert ingest_body["errors"] == []

    runs_response = client.get(
        f"/api/v1/business-data/sources/{source_id}/runs",
    )

    assert runs_response.status_code == 200
    runs_body = runs_response.json()
    assert runs_body["pagination"] == {
        "next_cursor": None,
        "has_more": False,
    }
    assert len(runs_body["data"]) == 1

    run = runs_body["data"][0]
    assert set(run) == {
        "id",
        "source_id",
        "schedule_id",
        "status",
        "triggered_by",
        "total_records",
        "succeeded_records",
        "failed_records",
        "error_reason",
        "error_detail",
        "started_at",
        "finished_at",
        "created_at",
    }
    assert run["id"] == ingest_body["run_id"]
    assert run["source_id"] == source_id
    assert run["schedule_id"] is None
    assert run["status"] == "completed"
    assert run["triggered_by"] == "upload"
    assert run["total_records"] == 2
    assert run["succeeded_records"] == 2
    assert run["failed_records"] == 0
    assert run["error_reason"] is None
    assert run["error_detail"] is None
    assert run["started_at"] is not None
    assert run["finished_at"] is not None


def test_ingest_returns_completed_with_errors_for_invalid_items(
    client: TestClient,
) -> None:
    source = _create_source(client)
    source_id = str(source["id"])

    response = client.post(
        f"/api/v1/business-data/sources/{source_id}/ingest",
        json={
            "triggered_by": "upload",
            "items": [
                {
                    "external_id": "SKU-VALID",
                    "fields": {"name": "Teh Hijau", "stock": 12},
                    "captured_at": "2026-09-18T10:05:00+07:00",
                },
                {
                    "external_id": "   ",
                    "fields": {"name": "Baris invalid", "stock": 0},
                    "captured_at": "2026-09-18T10:06:00+07:00",
                },
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed_with_errors"
    assert body["total_records"] == 2
    assert body["succeeded_records"] == 1
    assert body["failed_records"] == 1
    assert body["errors"] == [
        {
            "external_id": "   ",
            "reason": "external_id must be a non-empty string",
        }
    ]

    runs_response = client.get(
        f"/api/v1/business-data/sources/{source_id}/runs",
    )
    assert runs_response.status_code == 200

    run = runs_response.json()["data"][0]
    assert run["id"] == body["run_id"]
    assert run["status"] == "completed_with_errors"
    assert run["total_records"] == 2
    assert run["succeeded_records"] == 1
    assert run["failed_records"] == 1
    assert run["error_reason"] == "one or more records failed to persist"
    assert run["error_detail"] == {
        "item_errors": [
            {
                "external_id": "   ",
                "reason": "external_id must be a non-empty string",
            }
        ]
    }


def test_business_data_routes_return_404_for_unknown_source(
    client: TestClient,
) -> None:
    unknown_source_id = uuid.uuid4()

    ingest_response = client.post(
        f"/api/v1/business-data/sources/{unknown_source_id}/ingest",
        json={
            "items": [],
        },
    )
    assert ingest_response.status_code == 404
    assert ingest_response.json()["detail"] == "Business data source not found."

    runs_response = client.get(
        f"/api/v1/business-data/sources/{unknown_source_id}/runs",
    )
    assert runs_response.status_code == 404
    assert runs_response.json()["detail"] == "Business data source not found."


async def _create_isolated_business_data_source() -> str:
    from app.db.models.business_data_repository import create_business_data_source
    from app.db.models.identity import User, Workspace, WorkspaceMember
    from app.db.session import get_session

    suffix = uuid.uuid4().hex

    async with get_session() as session:
        user = User(
            email=f"business-data-isolation-{suffix}@example.test",
            password_hash="not-used-by-this-test",
            display_name="Business Data Isolation User",
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.flush()

        workspace = Workspace(
            name=f"Business Data Isolation {suffix}",
            slug=f"business-data-isolation-{suffix}",
            owner_user_id=user.id,
        )
        session.add(workspace)
        await session.flush()

        session.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=user.id,
                role="owner",
            )
        )

        source = await create_business_data_source(
            session,
            workspace_id=workspace.id,
            name="Private business data source",
            source_type="file_upload",
            adapter_type="csv",
            data_template="product_inventory",
        )
        await session.commit()
        return str(source.id)


def test_workspace_cannot_access_other_business_data_source(
    client: TestClient,
) -> None:
    from app.workers.async_bridge import run_async

    other_source_id = run_async(_create_isolated_business_data_source())

    ingest_response = client.post(
        f"/api/v1/business-data/sources/{other_source_id}/ingest",
        json={
            "items": [
                {
                    "external_id": "SKU-FORBIDDEN",
                    "fields": {"name": "Tidak boleh masuk"},
                    "captured_at": "2026-09-18T10:20:00+07:00",
                }
            ]
        },
    )
    assert ingest_response.status_code == 404
    assert ingest_response.json()["detail"] == "Business data source not found."

    runs_response = client.get(
        f"/api/v1/business-data/sources/{other_source_id}/runs",
    )
    assert runs_response.status_code == 404
    assert runs_response.json()["detail"] == "Business data source not found."


async def _create_pending_business_data_run(source_id: str) -> None:
    from app.db.models.business_data import BusinessDataRun
    from app.db.session import get_session

    async with get_session() as session:
        run = BusinessDataRun(
            business_data_source_id=uuid.UUID(source_id),
            status="pending",
            triggered_by="manual",
        )
        session.add(run)
        await session.commit()


def test_ingest_rejects_source_with_in_flight_run(
    client: TestClient,
) -> None:
    from app.workers.async_bridge import run_async

    source = _create_source(client)
    source_id = str(source["id"])

    run_async(_create_pending_business_data_run(source_id))

    response = client.post(
        f"/api/v1/business-data/sources/{source_id}/ingest",
        json={
            "items": [
                {
                    "external_id": "SKU-BLOCKED",
                    "fields": {"name": "Tidak boleh membuat run kedua"},
                    "captured_at": "2026-09-18T10:25:00+07:00",
                }
            ]
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Business data source already has a run in progress."
    )
