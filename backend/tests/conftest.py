"""Shared pytest fixtures for the backend test suite."""

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient

from app.db.session import dispose_engine_for_current_loop
from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
async def _dispose_db_engine_after_test() -> AsyncIterator[None]:
    """Doc 17 hotfix: app/db/session.py's engine cache is keyed by event
    loop, not by process (see that module's docstring for the full
    reasoning) -- which fixes cross-loop reuse, but only if every test's
    engine is actually disposed BEFORE pytest-asyncio closes that test's
    loop. Without this, a leftover cache entry survives its own loop's
    closing (harmless in isolation, since nothing else can reach a closed
    loop's entry again) but its pooled asyncpg connections/tasks never
    get a clean, awaited shutdown -- exactly the "leaked/unawaited
    cancellation coroutine" outcome this fixture exists to avoid.

    Autouse and applied globally (not just to `-m integration` tests, and
    not conditioned on which test requested it) so every test's loop --
    whether or not that particular test happened to touch the database --
    is cleaned up the same way, uniformly. A test that never touched the
    database simply has nothing cached to dispose (a no-op).
    """
    yield
    await dispose_engine_for_current_loop()
