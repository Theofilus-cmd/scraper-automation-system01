"""Shared test-data factories -- create durable sources/runs/tasks directly
against a real Postgres, bypassing the API/Celery-dispatch layers, for
tests that need a real row to attach to (a foreign key, an idempotency
check, a direct call to upsert_scrape_result()/scrape_source_url())
without either going through HTTP or waiting on a live Celery broker round
trip.

Every function opens and commits its own session/transaction (matching the
convention every repository module in app/db/models/ already uses), and
every one is safe to call from multiple `@pytest.mark.integration` tests
concurrently -- URLs/idempotency keys are always uuid4-suffixed so no two
calls, even within the same test run, ever collide on a real unique
constraint.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.db.models.lifecycle import Run, Task
from app.db.models.scraping import Source
from app.db.models.sources_repository import create_or_get_source
from app.db.session import get_session

MOCK_STORE_BASE_URL = "http://mock-store:4000"


def unique_source_url() -> str:
    return f"{MOCK_STORE_BASE_URL}/products/test-{uuid.uuid4().hex}"


async def create_test_source(*, url: str | None = None, status: str = "active") -> Source:
    """Creates a real `sources` row via the same `create_or_get_source()`
    path the API uses, so the create-time SSRF check (doc 18 §7.1) and the
    `normalized_url` partial-unique-index behavior (migration 0003) are
    exercised the same way here as in production. `url` always resolves to
    `mock-store` (the one SSRF-allowlisted host,
    app/core/config.py::ssrf_allowed_hosts) unless the caller passes a
    specific one, so this never needs network mocking.

    `status` is applied as a direct post-create update when it isn't
    `"active"` -- `create_or_get_source()` itself only ever creates
    `active` rows (doc 18 §6.1); archiving/pausing are separate,
    deliberate operations, not a create-time parameter.
    """
    async with get_session() as session:
        source, _created = await create_or_get_source(
            session, url=url or unique_source_url(), adapter_slug="mock_store"
        )
        if status != "active":
            source.status = status
            await session.commit()
        return source


async def create_test_run(
    source_id: uuid.UUID,
    *,
    status: str = "running",
    triggered_by: str = "manual",
    schedule_id: uuid.UUID | None = None,
    client_idempotency_key: str | None = None,
) -> Run:
    """Inserts a `runs` row directly -- bypasses
    `runs_repository.create_manual_run()`'s no-overlap pre-check/collision
    handling and Celery dispatch entirely, for tests that need to seed a
    specific run shape directly rather than drive it through the full
    trigger pipeline. Tests that exercise the no-overlap invariant itself
    call `create_manual_run()` (or a raw `INSERT`) directly instead of
    this factory.
    """
    async with get_session() as session:
        run = Run(
            source_id=source_id,
            schedule_id=schedule_id,
            status=status,
            triggered_by=triggered_by,
            client_idempotency_key=client_idempotency_key,
        )
        session.add(run)
        await session.commit()
        return run


async def create_test_task(
    run_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    status: str = "queued",
    idempotency_key: str | None = None,
    queued_at: datetime | None = None,
) -> Task:
    async with get_session() as session:
        task = Task(
            run_id=run_id,
            source_id=source_id,
            status=status,
            idempotency_key=idempotency_key or f"{run_id}:{source_id}:{uuid.uuid4().hex}",
            queued_at=queued_at if queued_at is not None else datetime.now(UTC),
        )
        session.add(task)
        await session.commit()
        return task


async def create_source_run_task(
    *,
    source_status: str = "active",
    run_status: str = "running",
    task_status: str = "queued",
) -> tuple[Source, Run, Task]:
    """The common case: a fresh source with one run and one task, ready to
    pass straight into `upsert_scrape_result()` or
    `scrape_source_url(str(task.id))`.
    """
    source = await create_test_source(status=source_status)
    run = await create_test_run(source.id, status=run_status)
    task = await create_test_task(run.id, source.id, status=task_status)
    return source, run, task
