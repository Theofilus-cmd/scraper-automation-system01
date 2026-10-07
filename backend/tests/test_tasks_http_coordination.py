"""Fast, DB-free coverage for HTTP task execution-lease integration."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.workers import tasks_http
from app.workers.coordination import CoordinationUnavailableError, TaskExecutionLease


class FakeTaskExecutionRedisClient:
    def __init__(self) -> None:
        self.closed = False

    async def set(self, name: str, value: str, *, ex: int, nx: bool) -> object:
        raise AssertionError("SET is mocked at the coordination boundary")

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object:
        raise AssertionError("EVAL is mocked at the coordination boundary")

    async def aclose(self) -> object:
        self.closed = True
        return None


@pytest.fixture
def task_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def lease(task_id: uuid.UUID) -> TaskExecutionLease:
    return TaskExecutionLease(
        key=f"scraper:task-execution:{task_id}",
        token="test-owner-token",
        ttl_seconds=60,
    )
@pytest.mark.asyncio
async def test_duplicate_lease_skips_the_scrape_pipeline(
    monkeypatch: pytest.MonkeyPatch, task_id: uuid.UUID
) -> None:
    client = FakeTaskExecutionRedisClient()
    pipeline_called = False

    async def acquire(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def pipeline(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal pipeline_called
        pipeline_called = True
        return {"status": "completed"}

    monkeypatch.setattr(tasks_http, "get_task_execution_redis_client", lambda: client)
    monkeypatch.setattr(tasks_http, "acquire_task_execution_lease", acquire)
    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", pipeline)

    result = await tasks_http._scrape_source_url_for_task_with_lease(task_id, attempt=1)

    assert result == {"status": "duplicate_execution"}
    assert pipeline_called is False
    assert client.closed is True


@pytest.mark.asyncio
async def test_success_releases_lease_and_closes_client(
    monkeypatch: pytest.MonkeyPatch, task_id: uuid.UUID, lease: TaskExecutionLease
) -> None:
    client = FakeTaskExecutionRedisClient()
    released: list[TaskExecutionLease] = []

    async def acquire(*_args: Any, **_kwargs: Any) -> TaskExecutionLease:
        return lease

    async def release(value: TaskExecutionLease, **_kwargs: Any) -> bool:
        released.append(value)
        return True

    async def pipeline(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "completed", "product_id": "product-1"}

    monkeypatch.setattr(tasks_http, "get_task_execution_redis_client", lambda: client)
    monkeypatch.setattr(tasks_http, "acquire_task_execution_lease", acquire)
    monkeypatch.setattr(tasks_http, "release_task_execution_lease", release)
    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", pipeline)

    result = await tasks_http._scrape_source_url_for_task_with_lease(task_id, attempt=1)

    assert result == {"status": "completed", "product_id": "product-1"}
    assert released == [lease]
    assert client.closed is True
@pytest.mark.asyncio
async def test_pipeline_error_still_releases_lease_and_closes_client(
    monkeypatch: pytest.MonkeyPatch, task_id: uuid.UUID, lease: TaskExecutionLease
) -> None:
    client = FakeTaskExecutionRedisClient()
    released: list[TaskExecutionLease] = []

    async def acquire(*_args: Any, **_kwargs: Any) -> TaskExecutionLease:
        return lease

    async def release(value: TaskExecutionLease, **_kwargs: Any) -> bool:
        released.append(value)
        return True

    async def pipeline(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("pipeline failed")

    monkeypatch.setattr(tasks_http, "get_task_execution_redis_client", lambda: client)
    monkeypatch.setattr(tasks_http, "acquire_task_execution_lease", acquire)
    monkeypatch.setattr(tasks_http, "release_task_execution_lease", release)
    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", pipeline)

    with pytest.raises(RuntimeError, match="pipeline failed"):
        await tasks_http._scrape_source_url_for_task_with_lease(task_id, attempt=1)

    assert released == [lease]
    assert client.closed is True


@pytest.mark.asyncio
async def test_acquire_error_propagates_without_running_pipeline(
    monkeypatch: pytest.MonkeyPatch, task_id: uuid.UUID
) -> None:
    client = FakeTaskExecutionRedisClient()
    pipeline_called = False

    async def acquire(*_args: Any, **_kwargs: Any) -> TaskExecutionLease:
        raise CoordinationUnavailableError("redis unavailable")

    async def pipeline(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal pipeline_called
        pipeline_called = True
        return {"status": "completed"}

    monkeypatch.setattr(tasks_http, "get_task_execution_redis_client", lambda: client)
    monkeypatch.setattr(tasks_http, "acquire_task_execution_lease", acquire)
    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", pipeline)

    with pytest.raises(CoordinationUnavailableError, match="redis unavailable"):
        await tasks_http._scrape_source_url_for_task_with_lease(task_id, attempt=1)

    assert pipeline_called is False
    assert client.closed is True
@pytest.mark.asyncio
async def test_release_error_does_not_replace_scrape_result(
    monkeypatch: pytest.MonkeyPatch, task_id: uuid.UUID, lease: TaskExecutionLease
) -> None:
    client = FakeTaskExecutionRedisClient()

    async def acquire(*_args: Any, **_kwargs: Any) -> TaskExecutionLease:
        return lease

    async def release(*_args: Any, **_kwargs: Any) -> bool:
        raise CoordinationUnavailableError("release unavailable")

    async def pipeline(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "completed"}

    monkeypatch.setattr(tasks_http, "get_task_execution_redis_client", lambda: client)
    monkeypatch.setattr(tasks_http, "acquire_task_execution_lease", acquire)
    monkeypatch.setattr(tasks_http, "release_task_execution_lease", release)
    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", pipeline)

    assert await tasks_http._scrape_source_url_for_task_with_lease(task_id, attempt=1) == {
        "status": "completed"
    }
    assert client.closed is True


@pytest.mark.parametrize("supervisor_fails", [False, True])
async def test_lease_wrapper_uses_supervisor_and_cleans_resources(
    monkeypatch: pytest.MonkeyPatch,
    task_id: uuid.UUID,
    lease: TaskExecutionLease,
    supervisor_fails: bool,
) -> None:
    client = FakeTaskExecutionRedisClient()
    supervised: list[uuid.UUID] = []
    released: list[TaskExecutionLease] = []

    async def acquire(*args: Any, **kwargs: Any) -> TaskExecutionLease:
        return lease

    async def supervise(
        received_id: uuid.UUID, **kwargs: Any
    ) -> dict[str, Any]:
        supervised.append(received_id)
        assert kwargs["attempt"] == 2
        assert kwargs["lease"] is lease
        assert kwargs["client"] is client
        if supervisor_fails:
            raise CoordinationUnavailableError("supervisor renewal failed")
        return {"status": "completed"}

    async def release(value: TaskExecutionLease, **kwargs: Any) -> bool:
        released.append(value)
        return True

    async def forbidden_pipeline(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("wrapper must call supervisor, not pipeline directly")

    monkeypatch.setattr(tasks_http, "get_task_execution_redis_client", lambda: client)
    monkeypatch.setattr(tasks_http, "acquire_task_execution_lease", acquire)
    monkeypatch.setattr(tasks_http, "release_task_execution_lease", release)
    monkeypatch.setattr(tasks_http, "_run_pipeline_with_lease_renewal", supervise)
    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", forbidden_pipeline)

    if supervisor_fails:
        with pytest.raises(CoordinationUnavailableError, match="supervisor renewal failed"):
            await tasks_http._scrape_source_url_for_task_with_lease(task_id, attempt=2)
    else:
        result = await tasks_http._scrape_source_url_for_task_with_lease(task_id, attempt=2)
        assert result == {"status": "completed"}

    assert supervised == [task_id]
    assert released == [lease]
    assert client.closed is True
