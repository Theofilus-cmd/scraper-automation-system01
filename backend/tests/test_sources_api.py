"""HTTP-layer integration tests for `/api/v1/sources` -- doc 18 §6.1,
§6.2's create/read/update/archive/unarchive/schedule/run-trigger surface,
exercised the same way a real caller would: through `client: TestClient`
against a real Postgres (and a real Redis for the one endpoint here that
dispatches to Celery, `POST /sources/{id}/runs` -- no worker is needed to
*consume* that dispatch, only the Postgres run/task rows and the HTTP
response body itself are ever asserted on here).

    docker compose exec api pytest -m integration

This Postgres is never truncated between tests (tests/factories.py's own
module docstring) -- every source this file creates uses a fresh,
uuid4-suffixed URL (factories.unique_source_url()) so nothing here ever
collides with another test's rows, but a couple of assertions below
(anything that lists `/sources` without a filter scoping it to this
test's own rows) are written as membership/no-repeat proofs over
whatever's really in the table, not as exact-set equality -- see the
individual tests for why each is still deterministic despite that.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.workers.async_bridge import run_async
from tests.factories import create_test_source, unique_source_url

pytestmark = pytest.mark.integration


def test_full_lifecycle_round_trip_through_the_api(client: TestClient) -> None:
    """doc 18 §6.1: create -> archive -> (still readable, now archived) ->
    unarchive -> pause -> reactivate, entirely through the HTTP surface.
    """
    url = unique_source_url()

    create_response = client.post("/api/v1/sources", json={"url": url})
    assert create_response.status_code == 201
    source_id = create_response.json()["id"]

    delete_response = client.delete(f"/api/v1/sources/{source_id}")
    assert delete_response.status_code == 204
    assert delete_response.content == b""

    archived_response = client.get(f"/api/v1/sources/{source_id}")
    assert archived_response.status_code == 200
    assert archived_response.json()["status"] == "archived"

    unarchive_response = client.post(f"/api/v1/sources/{source_id}/unarchive")
    assert unarchive_response.status_code == 200
    assert unarchive_response.json()["status"] == "active"

    pause_response = client.patch(f"/api/v1/sources/{source_id}", json={"status": "paused"})
    assert pause_response.status_code == 200
    assert pause_response.json()["status"] == "paused"

    reactivate_response = client.patch(f"/api/v1/sources/{source_id}", json={"status": "active"})
    assert reactivate_response.status_code == 200
    assert reactivate_response.json()["status"] == "active"


def test_patch_while_archived_returns_409_source_archived(client: TestClient) -> None:
    source = run_async(create_test_source(status="archived"))

    response = client.patch(f"/api/v1/sources/{source.id}", json={"status": "paused"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_ARCHIVED"


def test_unarchive_while_not_archived_returns_409_source_not_archived(client: TestClient) -> None:
    source = run_async(create_test_source(status="active"))

    response = client.post(f"/api/v1/sources/{source.id}/unarchive")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_NOT_ARCHIVED"


def test_schedule_upsert_while_archived_returns_409_source_archived(client: TestClient) -> None:
    source = run_async(create_test_source(status="archived"))

    response = client.put(f"/api/v1/sources/{source.id}/schedule", json={"interval_minutes": 30})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_ARCHIVED"


def test_trigger_run_collision_returns_409_with_matching_run_id(client: TestClient) -> None:
    """doc 18 §4.4/§6.2: a second trigger for a source that already has an
    in-flight run is rejected, and the rejection names the run that's
    already in flight -- proven here as exact id equality, not just "some
    run_id is present".
    """
    source = run_async(create_test_source())

    first_response = client.post(f"/api/v1/sources/{source.id}/runs")
    assert first_response.status_code == 202
    first_run_id = first_response.json()["run_id"]

    second_response = client.post(f"/api/v1/sources/{source.id}/runs")

    assert second_response.status_code == 409
    body = second_response.json()
    assert body["error"]["code"] == "SOURCE_RUN_IN_PROGRESS"
    assert body["error"]["details"]["run_id"] == first_run_id


def test_create_source_is_idempotent_via_http(client: TestClient) -> None:
    url = unique_source_url()

    first_response = client.post("/api/v1/sources", json={"url": url})
    second_response = client.post("/api/v1/sources", json={"url": url})

    assert first_response.status_code == 201
    assert second_response.status_code == 200
    assert first_response.json()["id"] == second_response.json()["id"]


def test_get_source_unknown_id_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/sources/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_list_sources_pagination_no_id_repeats_across_pages(client: TestClient) -> None:
    for _ in range(3):
        run_async(create_test_source())

    first_page = client.get("/api/v1/sources", params={"limit": 2})
    assert first_page.status_code == 200
    first_body = first_page.json()
    assert len(first_body["data"]) == 2
    assert first_body["pagination"]["has_more"] is True
    assert first_body["pagination"]["next_cursor"] is not None

    second_page = client.get(
        "/api/v1/sources",
        params={"limit": 2, "cursor": first_body["pagination"]["next_cursor"]},
    )
    assert second_page.status_code == 200
    second_body = second_page.json()

    first_ids = {row["id"] for row in first_body["data"]}
    second_ids = {row["id"] for row in second_body["data"]}
    assert first_ids.isdisjoint(second_ids)


def test_list_sources_status_filter(client: TestClient) -> None:
    """This table is shared across the whole suite (see module docstring),
    so this can't assert an exact result set -- it proves membership and
    exclusion instead, for two rows this test just created. A generous
    limit=100 plus newest-first ordering (created_at DESC) is what keeps
    that deterministic: nothing else is created between these two rows and
    the requests below, so both are guaranteed to land on the very first
    page of their own status's filtered results.
    """
    active_source = run_async(create_test_source(status="active"))
    archived_source = run_async(create_test_source(status="archived"))

    active_page = client.get("/api/v1/sources", params={"status": "active", "limit": 100})
    archived_page = client.get("/api/v1/sources", params={"status": "archived", "limit": 100})
    assert active_page.status_code == 200
    assert archived_page.status_code == 200

    active_ids = {row["id"] for row in active_page.json()["data"]}
    archived_ids = {row["id"] for row in archived_page.json()["data"]}

    assert str(active_source.id) in active_ids
    assert str(active_source.id) not in archived_ids
    assert str(archived_source.id) in archived_ids
    assert str(archived_source.id) not in active_ids


@pytest.mark.parametrize(
    ("interval_minutes", "expected_status", "expected_code"),
    [
        (30, 200, None),
        (14, 422, "SCHEDULE_INTERVAL_TOO_SHORT"),
        (10081, 422, "SCHEDULE_INTERVAL_TOO_LONG"),
    ],
)
def test_schedule_upsert_interval_bounds(
    client: TestClient,
    interval_minutes: int,
    expected_status: int,
    expected_code: str | None,
) -> None:
    source = run_async(create_test_source())

    response = client.put(
        f"/api/v1/sources/{source.id}/schedule", json={"interval_minutes": interval_minutes}
    )

    assert response.status_code == expected_status
    body = response.json()
    if expected_code is None:
        assert body["interval_minutes"] == interval_minutes
        assert body["is_active"] is True
    else:
        assert body["error"]["code"] == expected_code


def test_schedule_delete_is_idempotent_and_source_reports_null_schedule(
    client: TestClient,
) -> None:
    source = run_async(create_test_source())
    put_response = client.put(
        f"/api/v1/sources/{source.id}/schedule", json={"interval_minutes": 30}
    )
    assert put_response.status_code == 200

    first_delete = client.delete(f"/api/v1/sources/{source.id}/schedule")
    second_delete = client.delete(f"/api/v1/sources/{source.id}/schedule")
    assert first_delete.status_code == 204
    assert second_delete.status_code == 204

    get_response = client.get(f"/api/v1/sources/{source.id}")
    assert get_response.json()["schedule"] is None


def test_deprecation_header_absent_on_sources_responses(client: TestClient) -> None:
    """LegacyScrapesDeprecationMiddleware (app/api/v1/scrapes.py) only ever
    stamps a path starting with /api/v1/scrapes -- proven absent here
    across a success, a 404, and a 409, the same three-response-shape
    spread test_scrapes_integration.py uses to prove the header's
    *presence* on its own router.
    """
    source = run_async(create_test_source())

    success = client.get(f"/api/v1/sources/{source.id}")
    not_found = client.get(f"/api/v1/sources/{uuid.uuid4()}")
    conflict = client.post(f"/api/v1/sources/{source.id}/unarchive")  # active source -> 409

    assert success.status_code == 200
    assert not_found.status_code == 404
    assert conflict.status_code == 409

    assert success.headers.get("deprecation") is None
    assert not_found.headers.get("deprecation") is None
    assert conflict.headers.get("deprecation") is None


def test_request_id_header_present_on_success_and_error(client: TestClient) -> None:
    source = run_async(create_test_source())

    success = client.get(f"/api/v1/sources/{source.id}")
    error = client.get(f"/api/v1/sources/{uuid.uuid4()}")

    assert success.headers.get("x-request-id") is not None
    assert error.headers.get("x-request-id") is not None
