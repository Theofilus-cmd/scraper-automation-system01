"""Lease supervisor failure and cleanup tests."""

import asyncio
import uuid
from typing import Any

import pytest

from app.workers import tasks_http
from app.workers.coordination import (
    CoordinationUnavailableError,
    LeaseOwnershipLostError,
    TaskExecutionLease,
)


@pytest.mark.parametrize("failure", ["lost_owner", "redis_error"])
async def test_renewal_failure_cancels_and_awaits_pipeline_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    task_id = uuid.uuid4()
    lease = TaskExecutionLease(
        key=f"scraper:task-execution:{task_id}",
        token="owner-token",
        ttl_seconds=60,
    )

    async def pipeline(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
        return {"status": "completed"}

    async def renew(*args: Any, **kwargs: Any) -> bool:
        await started.wait()
        if failure == "redis_error":
            raise CoordinationUnavailableError("renewal Redis unavailable")
        return False

    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", pipeline)
    monkeypatch.setattr(
        tasks_http, "renew_task_execution_lease", renew, raising=False
    )

    expected_error = (
        LeaseOwnershipLostError
        if failure == "lost_owner"
        else CoordinationUnavailableError
    )
    with pytest.raises(expected_error) as exc_info:
        await asyncio.wait_for(
            tasks_http._run_pipeline_with_lease_renewal(
                task_id,
                attempt=1,
                lease=lease,
                client=object(),
                renewal_interval_seconds=0.001,
            ),
            timeout=2,
        )

    if failure == "redis_error":
        assert not isinstance(exc_info.value, LeaseOwnershipLostError)
    assert started.is_set()
    assert cleaned.is_set()


@pytest.mark.parametrize("pipeline_fails", [False, True])
async def test_pipeline_completion_stops_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    pipeline_fails: bool,
) -> None:
    renew_started = asyncio.Event()
    renew_cleaned = asyncio.Event()
    task_id = uuid.uuid4()
    lease = TaskExecutionLease(
        key=f"scraper:task-execution:{task_id}",
        token="owner-token",
        ttl_seconds=60,
    )

    async def pipeline(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await renew_started.wait()
        if pipeline_fails:
            raise RuntimeError("pipeline failed")
        return {"status": "completed"}

    async def renew(*args: Any, **kwargs: Any) -> bool:
        renew_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            renew_cleaned.set()
        return True

    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", pipeline)
    monkeypatch.setattr(tasks_http, "renew_task_execution_lease", renew)

    supervised = tasks_http._run_pipeline_with_lease_renewal(
        task_id,
        attempt=1,
        lease=lease,
        client=object(),
        renewal_interval_seconds=0.001,
    )

    if pipeline_fails:
        with pytest.raises(RuntimeError, match="pipeline failed"):
            await asyncio.wait_for(supervised, timeout=2)
    else:
        result = await asyncio.wait_for(supervised, timeout=2)
        assert result == {"status": "completed"}

    assert renew_cleaned.is_set()


async def test_external_cancellation_cleans_pipeline_and_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline_started = asyncio.Event()
    renew_started = asyncio.Event()
    pipeline_cleaned = asyncio.Event()
    renew_cleaned = asyncio.Event()
    task_id = uuid.uuid4()
    lease = TaskExecutionLease(
        key=f"scraper:task-execution:{task_id}",
        token="owner-token",
        ttl_seconds=60,
    )

    async def pipeline(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pipeline_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            pipeline_cleaned.set()
        return {"status": "completed"}

    async def renew(*args: Any, **kwargs: Any) -> bool:
        renew_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            renew_cleaned.set()
        return True

    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", pipeline)
    monkeypatch.setattr(tasks_http, "renew_task_execution_lease", renew)

    supervisor = asyncio.create_task(
        tasks_http._run_pipeline_with_lease_renewal(
            task_id,
            attempt=1,
            lease=lease,
            client=object(),
            renewal_interval_seconds=0.001,
        )
    )
    try:
        await asyncio.wait_for(pipeline_started.wait(), timeout=2)
        await asyncio.wait_for(renew_started.wait(), timeout=2)
        supervisor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await supervisor

        assert pipeline_cleaned.is_set()
        assert renew_cleaned.is_set()
    finally:
        if not supervisor.done():
            supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)


async def test_hanging_renewal_times_out_and_cleans_both_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline_started = asyncio.Event()
    pipeline_cleaned = asyncio.Event()
    renewal_cleaned = asyncio.Event()
    task_id = uuid.uuid4()
    lease = TaskExecutionLease(
        key=f"scraper:task-execution:{task_id}",
        token="owner-token",
        ttl_seconds=60,
    )

    async def pipeline(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pipeline_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            pipeline_cleaned.set()
        return {"status": "completed"}

    async def renew(*args: Any, **kwargs: Any) -> bool:
        await pipeline_started.wait()
        try:
            await asyncio.Event().wait()
        finally:
            renewal_cleaned.set()
        return True

    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", pipeline)
    monkeypatch.setattr(tasks_http, "renew_task_execution_lease", renew)

    with pytest.raises(CoordinationUnavailableError, match="timed out"):
        await asyncio.wait_for(
            tasks_http._run_pipeline_with_lease_renewal(
                task_id,
                attempt=1,
                lease=lease,
                client=object(),
                renewal_interval_seconds=0.001,
                renewal_timeout_seconds=0.01,
            ),
            timeout=2,
        )

    assert pipeline_cleaned.is_set()
    assert renewal_cleaned.is_set()


async def test_successful_renewals_keep_pipeline_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewed_twice = asyncio.Event()
    calls: list[TaskExecutionLease] = []
    task_id = uuid.uuid4()
    client = object()
    lease = TaskExecutionLease(
        key=f"scraper:task-execution:{task_id}",
        token="owner-token",
        ttl_seconds=60,
    )

    async def pipeline(
        received_id: uuid.UUID, *, attempt: int
    ) -> dict[str, Any]:
        assert received_id == task_id
        assert attempt == 2
        await renewed_twice.wait()
        return {"status": "completed"}

    async def renew(
        received_lease: TaskExecutionLease, **kwargs: Any
    ) -> bool:
        assert received_lease is lease
        assert kwargs["client"] is client
        calls.append(received_lease)
        if len(calls) >= 2:
            renewed_twice.set()
        return True

    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task", pipeline)
    monkeypatch.setattr(tasks_http, "renew_task_execution_lease", renew)

    result = await asyncio.wait_for(
        tasks_http._run_pipeline_with_lease_renewal(
            task_id,
            attempt=2,
            lease=lease,
            client=client,
            renewal_interval_seconds=0.001,
            renewal_timeout_seconds=0.1,
        ),
        timeout=2,
    )

    assert result == {"status": "completed"}
    assert len(calls) >= 2
