"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.db.models.identity import User
from app.db.session import dispose_engine_for_current_loop, get_session
from app.main import app
from app.workers.async_bridge import run_async
from tests.factories import TEST_USER_EMAIL, get_or_create_test_workspace


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


@pytest.fixture(autouse=True)
async def _dispose_db_engine_after_test() -> AsyncIterator[None]:
    """Dispose async engine resources after each pytest event loop."""
    yield
    await dispose_engine_for_current_loop()