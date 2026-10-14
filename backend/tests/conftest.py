"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.security import create_access_token
from app.db.models.identity import User
from app.db.session import dispose_engine_for_current_loop, get_session
from app.main import app
from app.workers.async_bridge import run_async
from tests.factories import (
    TEST_USER_EMAIL,
    TEST_WORKSPACE_SLUG,
    get_or_create_test_workspace,
)


async def _get_test_user() -> User:
    """Ensure the API test account and its workspace exist, then return it."""
    await get_or_create_test_workspace()

    async with get_session() as session:
        user = (
            await session.execute(
                __import__("sqlalchemy").select(User).where(
                    User.email == TEST_USER_EMAIL
                )
            )
        ).scalar_one()
        return user


@pytest.fixture
def client() -> Iterator[TestClient]:
    """An authenticated client scoped to the shared integration-test user."""
    user = run_async(_get_test_user())
    token = create_access_token(subject=str(user.id))

    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {token}"})
        yield test_client


_FIXTURE_URL_PATTERN = (
    r"^http://mock-store:4000/products/(recovery-)?test-[0-9a-f]{32}$"
)


async def _deactivate_fixture_schedules() -> None:
    """Stop test schedules from being claimed by a running scheduler."""
    async with get_session() as session:
        await session.execute(
            text(
                """
                UPDATE schedules AS sch
                SET is_active = false, updated_at = now()
                FROM sources AS s
                JOIN workspaces AS w ON w.id = s.workspace_id
                WHERE sch.source_id = s.id
                  AND sch.is_active
                  AND w.slug = :slug
                  AND s.url ~ :pattern
                """
            ),
            {"slug": TEST_WORKSPACE_SLUG, "pattern": _FIXTURE_URL_PATTERN},
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def _dispose_db_engine_after_test(
    request: pytest.FixtureRequest,
) -> AsyncIterator[None]:
    """Deactivate fixture schedules after integration tests, then dispose."""
    yield
    if request.node.get_closest_marker("integration") is not None:
        await _deactivate_fixture_schedules()
    await dispose_engine_for_current_loop()