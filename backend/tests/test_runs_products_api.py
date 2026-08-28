"""HTTP-layer integration tests for `/api/v1/runs` and `/api/v1/products`
-- doc 18 §6.2's run-read surface and §6.3's read-only product/observation-
history surface, exercised through `client: TestClient` against a real
Postgres (and a real Redis for the one trigger call this file makes
through `POST /sources/{id}/runs`, doc 18 §4.1's phase C -- no worker is
needed to consume that dispatch, only Postgres state and the HTTP response
bodies are asserted on).

    docker compose exec api pytest -m integration

Like test_sources_api.py, this Postgres accumulates rows across the whole
suite and is never truncated between tests. Unlike that file, though,
every list/filter/pagination assertion below is scoped to a fresh,
uuid4-random `source_id` this test itself created -- a real, hard filter
`GET /runs` and `GET /products` already support -- which is what lets
these assert exact result sets rather than mere membership.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db.models.repository import upsert_scrape_result
from app.db.models.scraping import Source
from app.scraping.types import NormalizedRecord, ValidationResult
from app.workers.async_bridge import run_async

from tests.factories import (
    create_isolated_workspace_source,
    create_test_run,
    create_test_source,
    create_test_task,
)
pytestmark = pytest.mark.integration


async def _seed_product_with_history(source: Source) -> uuid.UUID:
    """Seeds one product with two observation_history rows (a price change
    between them) via upsert_scrape_result() directly -- products.py's
    router is read-only by design (doc 18 §6.3): there is no
    product-creation HTTP endpoint, a product only ever comes from a real
    scrape.
    """
    run1 = await create_test_run(source.id, status="completed")
    task1 = await create_test_task(run1.id, source.id, status="succeeded")
    sku = f"API-TEST-{uuid.uuid4().hex}"
    first = await upsert_scrape_result(
        source_id=source.id,
        run_id=run1.id,
        task_id=task1.id,
        record=NormalizedRecord(
            product_url=source.url,
            scraped_at=datetime.now(UTC),
            stock_status="in_stock",
            product_name="API Test Widget",
            price=Decimal("10.00"),
            currency="USD",
            sku=sku,
        ),
        validation=ValidationResult(is_valid=True, errors={}),
    )

    run2 = await create_test_run(source.id, status="completed")
    task2 = await create_test_task(run2.id, source.id, status="succeeded")
    await upsert_scrape_result(
        source_id=source.id,
        run_id=run2.id,
        task_id=task2.id,
        record=NormalizedRecord(
            product_url=source.url,
            scraped_at=datetime.now(UTC),
            stock_status="in_stock",
            product_name="API Test Widget",
            price=Decimal("12.00"),
            currency="USD",
            sku=sku,
        ),
        validation=ValidationResult(is_valid=True, errors={}),
    )

    assert first.product_id is not None
    return first.product_id


def test_get_run_returns_run_to_dict_shape(client: TestClient) -> None:
    source = run_async(create_test_source())
    run = run_async(create_test_run(source.id, status="completed", triggered_by="manual"))

    response = client.get(f"/api/v1/runs/{run.id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "id",
        "source_id",
        "schedule_id",
        "status",
        "triggered_by",
        "total_tasks",
        "succeeded_tasks",
        "failed_tasks",
        "started_at",
        "finished_at",
        "created_at",
    }
    assert body["id"] == str(run.id)
    assert body["source_id"] == str(source.id)
    assert body["status"] == "completed"
    assert body["triggered_by"] == "manual"
    assert body["schedule_id"] is None


def test_get_run_unknown_id_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/runs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_list_run_tasks_returns_fixed_no_next_page_shape(client: TestClient) -> None:
    """doc 18 §6.2: cursor-paginated in shape only -- every run has at most
    one task today, so this is always {next_cursor: null, has_more: false}
    literally, never a real second page (app/api/v1/runs.py's own
    docstring).
    """
    source = run_async(create_test_source())
    run = run_async(create_test_run(source.id))
    task = run_async(create_test_task(run.id, source.id, status="succeeded"))

    response = client.get(f"/api/v1/runs/{run.id}/tasks")

    assert response.status_code == 200
    body = response.json()
    assert body["pagination"] == {"next_cursor": None, "has_more": False}
    assert len(body["data"]) == 1
    task_body = body["data"][0]
    assert set(task_body.keys()) == {
        "id",
        "run_id",
        "source_id",
        "status",
        "attempt_count",
        "max_attempts",
        "error_reason",
        "error_detail",
        "queued_at",
        "started_at",
        "finished_at",
    }
    assert task_body["id"] == str(task.id)
    assert task_body["run_id"] == str(run.id)
    assert task_body["source_id"] == str(source.id)
    assert task_body["status"] == "succeeded"


def test_list_run_tasks_unknown_run_id_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/runs/{uuid.uuid4()}/tasks")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_list_runs_pagination_and_filters(client: TestClient) -> None:
    """One fresh source, three runs spanning two statuses and both
    triggered_by values, every query scoped to this source's id -- an
    exact-set proof for source_id, status, and triggered_by each
    individually, plus the same no-repeat-across-pages cursor walk as
    test_sources_api.py, made exhaustive here (unlike that file) since the
    total row count under this source_id is known exactly.
    """
    source = run_async(create_test_source())
    run_a = run_async(create_test_run(source.id, status="completed", triggered_by="manual"))
    run_b = run_async(create_test_run(source.id, status="failed", triggered_by="schedule"))
    run_c = run_async(create_test_run(source.id, status="completed", triggered_by="schedule"))
    all_ids = {str(run_a.id), str(run_b.id), str(run_c.id)}

    scoped = client.get("/api/v1/runs", params={"source_id": str(source.id), "limit": 100})
    assert scoped.status_code == 200
    assert {row["id"] for row in scoped.json()["data"]} == all_ids

    by_status = client.get(
        "/api/v1/runs",
        params={"source_id": str(source.id), "status": "completed", "limit": 100},
    )
    assert {row["id"] for row in by_status.json()["data"]} == {str(run_a.id), str(run_c.id)}

    by_triggered_by = client.get(
        "/api/v1/runs",
        params={"source_id": str(source.id), "triggered_by": "schedule", "limit": 100},
    )
    assert {row["id"] for row in by_triggered_by.json()["data"]} == {str(run_b.id), str(run_c.id)}

    first_page = client.get("/api/v1/runs", params={"source_id": str(source.id), "limit": 2})
    first_body = first_page.json()
    assert len(first_body["data"]) == 2
    assert first_body["pagination"]["has_more"] is True
    assert first_body["pagination"]["next_cursor"] is not None

    second_page = client.get(
        "/api/v1/runs",
        params={
            "source_id": str(source.id),
            "limit": 2,
            "cursor": first_body["pagination"]["next_cursor"],
        },
    )
    second_body = second_page.json()
    assert second_body["pagination"] == {"next_cursor": None, "has_more": False}
    assert len(second_body["data"]) == 1

    first_ids = {row["id"] for row in first_body["data"]}
    second_ids = {row["id"] for row in second_body["data"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == all_ids


def test_triggered_run_agrees_with_get_run(client: TestClient) -> None:
    source = run_async(create_test_source())

    trigger_response = client.post(f"/api/v1/sources/{source.id}/runs")
    assert trigger_response.status_code == 202
    trigger_body = trigger_response.json()

    get_response = client.get(f"/api/v1/runs/{trigger_body['run_id']}")

    assert get_response.status_code == 200
    get_body = get_response.json()
    assert get_body["id"] == trigger_body["run_id"]
    assert get_body["source_id"] == str(source.id)
    assert get_body["status"] == trigger_body["status"]


def test_get_product_nests_current_observation(client: TestClient) -> None:
    source = run_async(create_test_source())
    product_id = run_async(_seed_product_with_history(source))

    response = client.get(f"/api/v1/products/{product_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(product_id)
    assert body["source_id"] == str(source.id)
    assert body["current_observation"] is not None
    assert body["current_observation"]["product_name"] == "API Test Widget"
    assert body["current_observation"]["price"] == "12.00"


def test_list_products_filters_by_source_id(client: TestClient) -> None:
    source = run_async(create_test_source())
    product_id = run_async(_seed_product_with_history(source))

    response = client.get("/api/v1/products", params={"source_id": str(source.id), "limit": 100})

    assert response.status_code == 200
    assert {row["id"] for row in response.json()["data"]} == {str(product_id)}


def test_product_history_pagination_is_newest_first_with_no_repeats(client: TestClient) -> None:
    """Unlike GET /runs/{id}/tasks, this endpoint has a real paging column
    (observation_history.version_created_at) -- limit=1 plus following
    next_cursor across both seeded rows must walk newest first and never
    repeat a row, doc 18 §6.3.
    """
    source = run_async(create_test_source())
    product_id = run_async(_seed_product_with_history(source))

    first_page = client.get(f"/api/v1/products/{product_id}/history", params={"limit": 1})
    assert first_page.status_code == 200
    first_body = first_page.json()
    assert len(first_body["data"]) == 1
    assert first_body["data"][0]["price"] == "12.00"  # the second (newer) seeded scrape
    assert first_body["pagination"]["has_more"] is True
    assert first_body["pagination"]["next_cursor"] is not None

    second_page = client.get(
        f"/api/v1/products/{product_id}/history",
        params={"limit": 1, "cursor": first_body["pagination"]["next_cursor"]},
    )
    assert second_page.status_code == 200
    second_body = second_page.json()
    assert len(second_body["data"]) == 1
    assert second_body["data"][0]["price"] == "10.00"  # the first (older) seeded scrape
    assert second_body["pagination"] == {"next_cursor": None, "has_more": False}

    first_ids = {row["id"] for row in first_body["data"]}
    second_ids = {row["id"] for row in second_body["data"]}
    assert first_ids.isdisjoint(second_ids)


def test_get_product_unknown_id_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/products/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


def test_get_product_history_unknown_product_id_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/products/{uuid.uuid4()}/history")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"
def test_workspace_cannot_read_other_workspace_runs_or_products(
    client: TestClient,
) -> None:
    """Every run/product read route must hide resources in another workspace."""
    _other_workspace, other_source = run_async(create_isolated_workspace_source())

    other_run = run_async(
        create_test_run(
            other_source.id,
            status="completed",
            triggered_by="manual",
        )
    )
    other_task = run_async(
        create_test_task(
            other_run.id,
            other_source.id,
            status="succeeded",
        )
    )
    other_product_id = run_async(_seed_product_with_history(other_source))

    list_runs = client.get(
        "/api/v1/runs",
        params={"source_id": str(other_source.id), "limit": 100},
    )
    assert list_runs.status_code == 200
    assert list_runs.json()["data"] == []

    get_run = client.get(f"/api/v1/runs/{other_run.id}")
    assert get_run.status_code == 404
    assert get_run.json()["error"]["code"] == "RUN_NOT_FOUND"

    run_tasks = client.get(f"/api/v1/runs/{other_run.id}/tasks")
    assert run_tasks.status_code == 404
    assert run_tasks.json()["error"]["code"] == "RUN_NOT_FOUND"

    list_products = client.get(
        "/api/v1/products",
        params={"source_id": str(other_source.id), "limit": 100},
    )
    assert list_products.status_code == 200
    assert list_products.json()["data"] == []

    get_product = client.get(f"/api/v1/products/{other_product_id}")
    assert get_product.status_code == 404
    assert get_product.json()["error"]["code"] == "PRODUCT_NOT_FOUND"

    product_history = client.get(f"/api/v1/products/{other_product_id}/history")
    assert product_history.status_code == 404
    assert product_history.json()["error"]["code"] == "PRODUCT_NOT_FOUND"

    legacy_task = client.get(f"/api/v1/scrapes/{other_task.id}")
    assert legacy_task.status_code == 404
    assert legacy_task.json()["error"]["code"] == "TASK_NOT_FOUND"