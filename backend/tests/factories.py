"""Shared durable test-data factories for integration tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models.identity import User, Workspace, WorkspaceMember
from app.db.models.lifecycle import Run, Task
from app.db.models.scraping import Source
from app.db.models.sources_repository import create_or_get_source
from app.db.session import get_session

MOCK_STORE_BASE_URL = "http://mock-store:4000"
TEST_USER_EMAIL = "integration-tests@example.test"
TEST_USER_PASSWORD = "integration-tests-password"
TEST_WORKSPACE_SLUG = "integration-tests"


def unique_source_url() -> str:
    return f"{MOCK_STORE_BASE_URL}/products/test-{uuid.uuid4().hex}"


async def get_or_create_test_workspace() -> Workspace:
    """Return the shared user-owned integration-test workspace."""
    async with get_session() as session:
        user = (
            await session.execute(select(User).where(User.email == TEST_USER_EMAIL))
        ).scalar_one_or_none()

        if user is None:
            user = User(
                email=TEST_USER_EMAIL,
                password_hash=hash_password(TEST_USER_PASSWORD),
                display_name="Integration Test User",
                is_active=True,
                is_verified=True,
            )
            session.add(user)
            await session.flush()

        workspace = (
            await session.execute(
                select(Workspace).where(Workspace.slug == TEST_WORKSPACE_SLUG)
            )
        ).scalar_one_or_none()

        if workspace is None:
            workspace = Workspace(
                name="Integration Tests",
                slug=TEST_WORKSPACE_SLUG,
                owner_user_id=user.id,
            )
            session.add(workspace)
            await session.flush()

        membership = (
            await session.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace.id,
                    WorkspaceMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()

        if membership is None:
            session.add(
                WorkspaceMember(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role="owner",
                )
            )

        await session.commit()
        await session.refresh(workspace)
        return workspace

async def create_isolated_workspace_source() -> tuple[Workspace, Source]:
    """Create a source owned by a separate user/workspace for isolation tests."""
    suffix = uuid.uuid4().hex
    email = f"workspace-isolation-{suffix}@example.test"
    slug = f"workspace-isolation-{suffix}"

    async with get_session() as session:
        user = User(
            email=email,
            password_hash=hash_password("workspace-isolation-password"),
            display_name="Workspace Isolation User",
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.flush()

        workspace = Workspace(
            name=f"Workspace Isolation {suffix}",
            slug=slug,
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

        await session.commit()
        await session.refresh(workspace)

    async with get_session() as session:
        source, _created = await create_or_get_source(
            session,
            workspace_id=workspace.id,
            url=unique_source_url(),
            adapter_slug="mock_store",
        )
        return workspace, source
async def create_test_source(
    *,
    url: str | None = None,
    status: str = "active",
) -> Source:
    """Create a source in the shared integration-test workspace."""
    workspace = await get_or_create_test_workspace()

    async with get_session() as session:
        source, _created = await create_or_get_source(
            session,
            workspace_id=workspace.id,
            url=url or unique_source_url(),
            adapter_slug="mock_store",
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
    """Insert a run directly for test setup."""
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
    """Insert a task directly for test setup."""
    async with get_session() as session:
        task = Task(
            run_id=run_id,
            source_id=source_id,
            status=status,
            idempotency_key=(
                idempotency_key
                or f"{run_id}:{source_id}:{uuid.uuid4().hex}"
            ),
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
    """Create one source, run, and task for an integration-test scenario."""
    source = await create_test_source(status=source_status)
    run = await create_test_run(source.id, status=run_status)
    task = await create_test_task(run.id, source.id, status=task_status)
    return source, run, task


async def finalize_run_and_task(
    run_id: uuid.UUID,
    task_id: uuid.UUID,
    *,
    run_status: str = "completed",
    task_status: str = "succeeded",
) -> None:
    """Transition a test run and task into terminal states."""
    async with get_session() as session:
        run = (
            await session.execute(select(Run).where(Run.id == run_id))
        ).scalar_one()
        task = (
            await session.execute(select(Task).where(Task.id == task_id))
        ).scalar_one()

        now = datetime.now(UTC)
        run.status = run_status
        run.finished_at = now
        task.status = task_status
        task.finished_at = now

        await session.commit()