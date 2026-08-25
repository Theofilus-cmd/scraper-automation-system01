"""Live-endpoint integration tests for POST/GET /api/v1/scrapes -- real
Postgres, Redis, and a real worker-http process consuming the actual
`http` queue (task_always_eager=False; doc 12 §1 -- eager mode would hide
real serialization/routing bugs). Requires the full default-profile
Compose stack running:

    docker compose exec api pytest -m integration

Targets mock-store's 4 fixed slugs over the real Compose network (see
tools/mock-store/app.py) -- the one test file that genuinely exercises
fetch()/parse() over real HTTP, not a mocked transport.
"""

import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

_MOCK_STORE_BASE_URL = "http://mock-store:4000"


def _poll_until_done(
    client: TestClient, task_id: str, *, timeout_seconds: float = 40.0
) -> tuple[int, dict[str, Any]]:
    """The endpoint itself already waits up to ~35s server-side before
    falling back to 202; this is a small client-side safety margin for a
    task that took slightly longer than that wait budget.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/scrapes/{task_id}")
        body: dict[str, Any] = response.json()
        if response.status_code != 202 and body.get("status") != "pending":
            return response.status_code, body
        time.sleep(1.0)
    raise AssertionError(f"task {task_id} did not finish within {timeout_seconds}s")


def _post_and_resolve(client: TestClient, source_url: str) -> tuple[int, dict[str, Any]]:
    """POST /api/v1/scrapes, following through a 202-pending response (if
    any) via GET polling until a terminal outcome is reached.
    """
    response = client.post("/api/v1/scrapes", json={"source_url": source_url})
    if response.status_code == 202:
        return _poll_until_done(client, response.json()["task_id"])
    return response.status_code, response.json()


def test_first_sighting_success_persists_and_returns_full_observation(
    client: TestClient,
) -> None:
    source_url = f"{_MOCK_STORE_BASE_URL}/products/widget-in-stock"

    status_code, body = _post_and_resolve(client, source_url)

    assert status_code == 200
    assert body["status"] == "completed"
    assert body["source"]["url"] == source_url
    assert body["observation"]["product_name"] == "Acme Widget - Blue"
    assert body["observation"]["stock_status"] == "in_stock"
    assert body["observation"]["is_valid"] is True


def test_repeat_scrape_is_idempotent_same_product_id(client: TestClient) -> None:
    source_url = f"{_MOCK_STORE_BASE_URL}/products/widget-out-of-stock"

    first_status, first_body = _post_and_resolve(client, source_url)
    second_status, second_body = _post_and_resolve(client, source_url)

    assert first_status == 200
    assert second_status == 200
    assert first_body["product"]["id"] == second_body["product"]["id"]
    assert second_body["created"] is False
    assert second_body["observation"]["stock_status"] == "out_of_stock"


def test_missing_required_field_returns_422(client: TestClient) -> None:
    source_url = f"{_MOCK_STORE_BASE_URL}/products/widget-missing-price"

    status_code, body = _post_and_resolve(client, source_url)

    assert status_code == 422
    assert body["error"]["code"] == "MISSING_REQUIRED_FIELD"


def test_partial_success_returns_200_with_validation_errors(client: TestClient) -> None:
    source_url = f"{_MOCK_STORE_BASE_URL}/products/widget-invalid-review-count"

    status_code, body = _post_and_resolve(client, source_url)

    assert status_code == 200
    assert body["status"] == "completed"
    assert body["observation"]["is_valid"] is True
    assert "review_count" in body["observation"]["validation_errors"]


def test_unsupported_adapter_returns_422_without_dispatching(client: TestClient) -> None:
    response = client.post(
        "/api/v1/scrapes", json={"source_url": "https://real-marketplace.example.com/p/1"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_ADAPTER"


def test_get_scrape_unknown_task_id_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/scrapes/{uuid.uuid4()}")
    assert response.status_code == 404
