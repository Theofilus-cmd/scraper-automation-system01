"""Integration test for /readyz -- requires a live Postgres + Redis.

Run inside the api container (or any environment with DATABASE_URL /
REDIS_URL pointing at real, reachable services):

    docker compose exec api pytest -m integration
"""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
def test_readyz_reports_healthy_dependencies(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert body["redis"] is True
