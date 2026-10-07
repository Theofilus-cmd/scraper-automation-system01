"""Durable recovery of an execution deferred by coordination failure."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import runs_repository
from app.db.models.lifecycle import Run, Task
from app.db.session import get_session
from app.workers import tasks_http
from app.workers.async_bridge import run_async
from app.workers.coordination import CoordinationUnavailableError
from tests.factories import (
    MOCK_STORE_BASE_URL,
    create_test_run,
    create_test_source,
    create_test_task,
)

pytestmark = pytest.mark.integration


def test_deferred_in_progress_task_is_requeued_with_same_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[uuid.UUID] = []

    async def setup() -> tuple[uuid.UUID, uuid.UUID]:
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/recovery-test-{uuid.uuid4().hex}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            row.status = "in_progress"
            row.attempt_count = 1
            row.queued_at = datetime.now(UTC) - timedelta(minutes=11)
            await session.commit()

        return task.id, run.id

    async def state(
        task_id: uuid.UUID, run_id: uuid.UUID
    ) -> tuple[str, int, str, int, int]:
        async with get_session() as session:
            task = (
                await session.execute(select(Task).where(Task.id == task_id))
            ).scalar_one()
            run = (
                await session.execute(select(Run).where(Run.id == run_id))
            ).scalar_one()
            return (
                task.status,
                task.attempt_count,
                run.status,
                run.succeeded_tasks,
                run.failed_tasks,
            )

    async def unavailable(*args: object, **kwargs: object) -> dict[str, object]:
        raise CoordinationUnavailableError("renewal Redis unavailable")

    task_id, run_id = run_async(setup())
    before = run_async(state(task_id, run_id))

    monkeypatch.setattr(
        tasks_http, "_scrape_source_url_for_task_with_lease", unavailable
    )
    monkeypatch.setattr(runs_repository, "_dispatch", dispatched.append)

    result = tasks_http.scrape_source_url(str(task_id))

    assert result == {
        "status": "deferred",
        "reason": "coordination_unavailable",
    }
    assert run_async(state(task_id, run_id)) == before

    summary = run_async(
        runs_repository.reconciliation_sweep(stuck_after=timedelta(minutes=10))
    )

    assert summary["requeued_tasks"] >= 1
    assert dispatched.count(task_id) == 1

    after = run_async(state(task_id, run_id))
    assert after[0] == "queued"
    assert after[1:] == before[1:]


def test_reconciliation_skips_task_with_active_execution_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers.coordination import (
        acquire_task_execution_lease,
        get_task_execution_redis_client,
        release_task_execution_lease,
    )

    dispatched: list[uuid.UUID] = []
    monkeypatch.setattr(runs_repository, "_dispatch", dispatched.append)

    async def exercise() -> None:
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/recovery-test-{uuid.uuid4().hex}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            row.status = "in_progress"
            row.attempt_count = 1
            row.queued_at = datetime.now(UTC) - timedelta(minutes=11)
            original_queued_at = row.queued_at
            await session.commit()

        client = get_task_execution_redis_client()
        lease = None
        try:
            lease = await acquire_task_execution_lease(task.id, client=client)
            assert lease is not None

            await runs_repository.reconciliation_sweep(
                stuck_after=timedelta(minutes=10)
            )

            assert task.id not in dispatched

            async with get_session() as session:
                row = (
                    await session.execute(select(Task).where(Task.id == task.id))
                ).scalar_one()
                assert row.status == "in_progress"
                assert row.attempt_count == 1
                assert row.queued_at == original_queued_at
        finally:
            try:
                if lease is not None:
                    await release_task_execution_lease(lease, client=client)
            finally:
                await client.aclose()

    run_async(exercise())


def test_reconciliation_redis_failure_preserves_task_and_does_not_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers import coordination

    dispatched: list[uuid.UUID] = []
    checked: list[uuid.UUID] = []
    monkeypatch.setattr(runs_repository, "_dispatch", dispatched.append)

    async def unavailable(
        task_id: uuid.UUID, *, client: object
    ) -> bool:
        checked.append(task_id)
        raise CoordinationUnavailableError("lease check Redis unavailable")

    monkeypatch.setattr(
        coordination, "task_execution_lease_exists", unavailable
    )

    async def exercise() -> None:
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/recovery-test-{uuid.uuid4().hex}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)

        original_queued_at = datetime.now(UTC) - timedelta(minutes=11)

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            row.status = "in_progress"
            row.attempt_count = 1
            row.queued_at = original_queued_at
            await session.commit()

        with pytest.raises(
            CoordinationUnavailableError, match="lease check Redis unavailable"
        ):
            await runs_repository.reconciliation_sweep(
                stuck_after=timedelta(minutes=10)
            )

        assert checked
        assert dispatched == []

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            assert row.status == "in_progress"
            assert row.attempt_count == 1
            assert row.queued_at == original_queued_at

    run_async(exercise())


def test_reconciliation_skips_locked_task_then_recovers_after_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    dispatched: list[uuid.UUID] = []
    monkeypatch.setattr(runs_repository, "_dispatch", dispatched.append)

    async def exercise() -> None:
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/recovery-test-{uuid.uuid4().hex}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)
        original_queued_at = datetime.now(UTC) - timedelta(minutes=11)

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            row.status = "in_progress"
            row.queued_at = original_queued_at
            await session.commit()

        async with get_session() as owner_session:
            locked = (
                await owner_session.execute(
                    select(Task).where(Task.id == task.id).with_for_update()
                )
            ).scalar_one()

            await asyncio.wait_for(
                runs_repository.reconciliation_sweep(
                    stuck_after=timedelta(minutes=10)
                ),
                timeout=5,
            )

            assert task.id not in dispatched
            assert locked.status == "in_progress"
            assert locked.queued_at == original_queued_at
            await owner_session.rollback()

        await runs_repository.reconciliation_sweep(
            stuck_after=timedelta(minutes=10)
        )
        assert dispatched.count(task.id) == 1

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            assert row.status == "queued"

    run_async(exercise())


def test_recovered_task_start_does_not_reset_durable_attempt_count() -> None:
    async def exercise() -> None:
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/recovery-test-{uuid.uuid4().hex}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            row.status = "queued"
            row.attempt_count = 2
            row.max_attempts = 3
            await session.commit()

        # A fresh recovery message supplies attempt=1, not the DB count.
        started = await tasks_http._mark_task_started(task.id, attempt=1)
        assert started is True

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            assert row.status == "in_progress"
            assert row.attempt_count == 3
            assert row.attempt_count <= row.max_attempts

    run_async(exercise())


def test_recovered_exhausted_task_dead_letters_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_detect(*args: object, **kwargs: object) -> None:
        raise AssertionError("exhausted task must finalize before adapter detection/fetch")

    monkeypatch.setattr(tasks_http.registry, "detect", forbidden_detect)

    async def exercise() -> None:
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/recovery-test-{uuid.uuid4().hex}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            row.status = "queued"
            row.attempt_count = 3
            row.max_attempts = 3
            row.error_reason = "network_error"
            row.error_detail = {"message": "fetch failed on last allowed attempt"}
            await session.commit()

        # Test the durable guard directly, without needing a Redis lease.
        result = await tasks_http._scrape_source_url_for_task(task.id, attempt=1)

        assert result == {
            "status": "failed",
            "error_reason": "network_error",
            "dead_letter": True,
        }

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            parent = (
                await session.execute(select(Run).where(Run.id == run.id))
            ).scalar_one()

            assert row.status == "dead_letter"
            assert row.attempt_count == 3
            assert row.finished_at is not None
            assert parent.status == "failed"
            assert parent.failed_tasks == 1
            assert parent.succeeded_tasks == 0

    run_async(exercise())


def test_last_durable_fetch_failure_dead_letters_with_fresh_message_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    fetched: list[str] = []

    class FakeFetchError(Exception):
        reason = "network_error"
        retry_after = None

    async def fail_fetch(url: str, context: object) -> None:
        fetched.append(url)
        raise FakeFetchError("last allowed fetch failed")

    adapter = SimpleNamespace(fetch=fail_fetch)
    monkeypatch.setattr(tasks_http, "FetchError", FakeFetchError)
    monkeypatch.setattr(tasks_http.registry, "detect", lambda url: adapter)

    async def exercise() -> None:
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/recovery-test-{uuid.uuid4().hex}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            row.status = "queued"
            row.attempt_count = 2
            row.max_attempts = 3
            await session.commit()

        result = await tasks_http._scrape_source_url_for_task(task.id, attempt=1)

        assert fetched == [source.url]
        assert result == {
            "status": "failed",
            "error_reason": "network_error",
            "dead_letter": True,
        }

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            parent = (
                await session.execute(select(Run).where(Run.id == run.id))
            ).scalar_one()
            assert row.attempt_count == 3
            assert row.status == "dead_letter"
            assert row.error_reason == "network_error"
            assert row.finished_at is not None
            assert parent.status == "failed"
            assert parent.failed_tasks == 1
            assert parent.succeeded_tasks == 0

    run_async(exercise())


def test_retrying_persists_not_before_without_changing_attempt_budget() -> None:
    async def exercise() -> None:
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/recovery-test-{uuid.uuid4().hex}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            row.status = "in_progress"
            row.attempt_count = 1
            row.max_attempts = 3
            await session.commit()

        before = datetime.now(UTC)
        changed = await tasks_http._mark_task_retrying(
            task.id,
            reason="network_error",
            message="fetch failed",
            retry_delay=600,
        )
        after = datetime.now(UTC)
        assert changed is True

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            assert row.status == "retrying"
            assert row.attempt_count == 1
            assert row.max_attempts == 3
            assert row.error_reason == "network_error"
            assert row.error_detail is not None
            assert row.error_detail["message"] == "fetch failed"

            value = row.error_detail["retry_not_before"]
            assert isinstance(value, str)
            deadline = datetime.fromisoformat(value)
            assert before + timedelta(seconds=600) <= deadline
            assert deadline <= after + timedelta(seconds=600)
            assert row.queued_at is not None
            assert before <= row.queued_at <= after

    run_async(exercise())


@pytest.mark.parametrize(
    ("deadline_case", "should_requeue"),
    [
        ("expired", True),
        ("within_grace", False),
        ("future", False),
        ("missing", False),
        ("invalid", False),
    ],
)
def test_retrying_recovery_respects_deadline_and_grace(
    monkeypatch: pytest.MonkeyPatch,
    deadline_case: str,
    should_requeue: bool,
) -> None:
    dispatched: list[uuid.UUID] = []
    monkeypatch.setattr(runs_repository, "_dispatch", dispatched.append)

    async def exercise() -> None:
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/recovery-test-{uuid.uuid4().hex}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)

        now = datetime.now(UTC)
        original_queued_at = now - timedelta(minutes=30)
        detail: dict[str, object] = {"message": "fetch failed"}

        if deadline_case == "expired":
            detail["retry_not_before"] = (
                now - timedelta(minutes=11)
            ).isoformat()
        elif deadline_case == "within_grace":
            detail["retry_not_before"] = (
                now - timedelta(minutes=5)
            ).isoformat()
        elif deadline_case == "future":
            detail["retry_not_before"] = (
                now + timedelta(minutes=10)
            ).isoformat()
        elif deadline_case == "invalid":
            detail["retry_not_before"] = "not-a-timestamp"

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            row.status = "retrying"
            row.attempt_count = 1
            row.max_attempts = 3
            row.queued_at = original_queued_at
            row.error_reason = "network_error"
            row.error_detail = detail
            await session.commit()

        await runs_repository.reconciliation_sweep(
            stuck_after=timedelta(minutes=10)
        )

        assert dispatched.count(task.id) == int(should_requeue)

        async with get_session() as session:
            row = (
                await session.execute(select(Task).where(Task.id == task.id))
            ).scalar_one()
            parent = (
                await session.execute(select(Run).where(Run.id == run.id))
            ).scalar_one()

            assert row.status == ("queued" if should_requeue else "retrying")
            assert row.attempt_count == 1
            assert row.max_attempts == 3
            assert row.error_reason == "network_error"
            assert row.error_detail == detail
            assert parent.failed_tasks == 0
            assert parent.succeeded_tasks == 0

            if should_requeue:
                assert row.queued_at is not None
                assert row.queued_at > original_queued_at
            else:
                assert row.queued_at == original_queued_at

    run_async(exercise())
