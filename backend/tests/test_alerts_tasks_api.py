"""`GET /api/v1/alerts` and `GET /api/v1/tasks` -- auth, filters, isolation.

    docker compose exec api pytest -m integration tests/test_alerts_tasks_api.py
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db.models.alerts import Alert
from app.db.models.repository import upsert_scrape_result
from app.db.models.scraping import Source
from app.db.session import get_session
from app.main import app
from app.scraping.types import NormalizedRecord, ValidationResult
from app.workers.async_bridge import run_async
from tests.factories import (
    create_isolated_workspace_source,
    create_test_run,
    create_test_source,
    create_test_task,
)

pytestmark = pytest.mark.integration


async def _seed_alert(source: Source, *, kind: str = "price_changed") -> Alert:
    run = await create_test_run(source.id, status="completed")
    task = await create_test_task(run.id, source.id, status="succeeded")
    result = await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=NormalizedRecord(
            product_url=source.url,
            scraped_at=datetime.now(UTC),
            stock_status="in_stock",
            product_name="Alert API Widget",
            price=Decimal("5.00"),
            currency="USD",
            sku=f"ALERT-API-{uuid.uuid4().hex}",
        ),
        validation=ValidationResult(is_valid=True, errors={}),
    )
    assert result.product_id is not None
    async with get_session() as session:
        alert = Alert(
            workspace_id=source.workspace_id,
            source_id=source.id,
            product_id=result.product_id,
            run_id=run.id,
            task_id=task.id,
            kind=kind,
            product_name="Alert API Widget",
            product_url=source.url,
            currency="USD",
            old_value="5.00",
            new_value="6.00",
            dedupe_key=f"api-test-{uuid.uuid4().hex}",
        )
        session.add(alert)
        await session.commit()
        return alert


def test_alerts_and_tasks_require_authentication() -> None:
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/v1/alerts").status_code == 401
        assert anonymous.get("/api/v1/tasks").status_code == 401


def test_list_alerts_filters_and_paginates(client: TestClient) -> None:
    source = run_async(create_test_source())
    run_async(_seed_alert(source, kind="price_changed"))
    run_async(_seed_alert(source, kind="stock_status_changed"))

    body = client.get("/api/v1/alerts", params={"source_id": str(source.id)}).json()
    assert len(body["data"]) == 2
    assert {a["kind"] for a in body["data"]} == {"price_changed", "stock_status_changed"}
    assert body["data"][0]["old_value"] == "5.00"

    only_stock = client.get(
        "/api/v1/alerts", params={"source_id": str(source.id), "kind": "stock_status_changed"}
    ).json()
    assert [a["kind"] for a in only_stock["data"]] == ["stock_status_changed"]

    page1 = client.get("/api/v1/alerts", params={"source_id": str(source.id), "limit": 1}).json()
    assert page1["pagination"]["has_more"] is True
    page2 = client.get(
        "/api/v1/alerts",
        params={
            "source_id": str(source.id),
            "limit": 1,
            "cursor": page1["pagination"]["next_cursor"],
        },
    ).json()
    assert page2["data"][0]["id"] != page1["data"][0]["id"]
    assert page2["pagination"]["has_more"] is False


def test_list_alerts_rejects_invalid_kind_and_cursor(client: TestClient) -> None:
    assert client.get("/api/v1/alerts", params={"kind": "bogus"}).status_code == 422
    assert client.get("/api/v1/alerts", params={"cursor": "!!"}).status_code == 422


def test_list_alerts_hides_other_workspace(client: TestClient) -> None:
    _ws, other_source = run_async(create_isolated_workspace_source())
    run_async(_seed_alert(other_source))

    response = client.get("/api/v1/alerts", params={"source_id": str(other_source.id)})
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_list_tasks_dead_letter_filter_and_isolation(client: TestClient) -> None:
    source = run_async(create_test_source())
    run = run_async(create_test_run(source.id, status="failed"))
    dead = run_async(create_test_task(run.id, source.id, status="dead_letter"))
    run2 = run_async(create_test_run(source.id, status="completed"))
    run_async(create_test_task(run2.id, source.id, status="succeeded"))

    body = client.get(
        "/api/v1/tasks", params={"status": "dead_letter", "source_id": str(source.id)}
    ).json()
    assert [t["id"] for t in body["data"]] == [str(dead.id)]

    _ws, other_source = run_async(create_isolated_workspace_source())
    other_run = run_async(create_test_run(other_source.id, status="failed"))
    run_async(create_test_task(other_run.id, other_source.id, status="dead_letter"))
    hidden = client.get(
        "/api/v1/tasks", params={"status": "dead_letter", "source_id": str(other_source.id)}
    ).json()
    assert hidden["data"] == []

    assert client.get("/api/v1/tasks", params={"status": "bogus"}).status_code == 422
