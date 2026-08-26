"""Fast, Redis-free unit tests for worker execution-lease coordination."""

from __future__ import annotations

import uuid

import pytest

from app.workers.coordination import (
    CoordinationUnavailableError,
    TaskExecutionLease,
    acquire_task_execution_lease,
    release_task_execution_lease,
)


class FakeLeaseRedisClient:
    def __init__(
        self,
        *,
        set_result: object = True,
        eval_results: list[object] | None = None,
        set_error: Exception | None = None,
        eval_error: Exception | None = None,
    ) -> None:
        self.set_result = set_result
        self.eval_results = list(eval_results or [])
        self.set_error = set_error
        self.eval_error = eval_error
        self.set_calls: list[tuple[str, str, int, bool]] = []
        self.eval_calls: list[tuple[str, int, tuple[str, ...]]] = []

    async def set(self, name: str, value: str, *, ex: int, nx: bool) -> object:
        self.set_calls.append((name, value, ex, nx))
        if self.set_error is not None:
            raise self.set_error
        return self.set_result

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object:
        self.eval_calls.append((script, numkeys, keys_and_args))
        if self.eval_error is not None:
            raise self.eval_error
        if not self.eval_results:
            raise AssertionError("unexpected Redis EVAL call")
        return self.eval_results.pop(0)

    async def aclose(self) -> object:
        return None
@pytest.mark.asyncio
async def test_acquire_uses_atomic_set_nx_with_configured_ttl() -> None:
    client = FakeLeaseRedisClient()
    task_id = uuid.uuid4()

    lease = await acquire_task_execution_lease(task_id, client=client)

    assert lease is not None
    assert lease.key == f"scraper:task-execution:{task_id}"
    assert lease.ttl_seconds == 60
    assert client.set_calls == [(lease.key, lease.token, 60, True)]
    assert uuid.UUID(lease.token)


@pytest.mark.asyncio
async def test_acquire_returns_none_when_another_worker_owns_the_lease() -> None:
    client = FakeLeaseRedisClient(set_result=None)

    assert await acquire_task_execution_lease(uuid.uuid4(), client=client) is None
    assert len(client.set_calls) == 1


@pytest.mark.asyncio
async def test_acquire_wraps_redis_failure_as_coordination_unavailable() -> None:
    client = FakeLeaseRedisClient(set_error=ConnectionError("redis unavailable"))

    with pytest.raises(CoordinationUnavailableError, match="could not acquire"):
        await acquire_task_execution_lease(uuid.uuid4(), client=client)


@pytest.mark.asyncio
async def test_release_deletes_only_the_current_owner_token() -> None:
    client = FakeLeaseRedisClient(eval_results=[1])
    lease = TaskExecutionLease("scraper:task-execution:abc", "owner-token", 60)

    assert await release_task_execution_lease(lease, client=client) is True
    script, numkeys, args = client.eval_calls[0]
    assert 'redis.call("GET", KEYS[1])' in script
    assert 'redis.call("DEL", KEYS[1])' in script
    assert numkeys == 1
    assert args == (lease.key, lease.token)


@pytest.mark.asyncio
async def test_release_returns_false_for_a_stale_owner_token() -> None:
    client = FakeLeaseRedisClient(eval_results=[0])
    lease = TaskExecutionLease("scraper:task-execution:abc", "stale-token", 60)

    assert await release_task_execution_lease(lease, client=client) is False


@pytest.mark.asyncio
async def test_release_wraps_redis_failure_as_coordination_unavailable() -> None:
    client = FakeLeaseRedisClient(eval_error=ConnectionError("redis unavailable"))
    lease = TaskExecutionLease("scraper:task-execution:abc", "owner-token", 60)

    with pytest.raises(CoordinationUnavailableError, match="could not release"):
        await release_task_execution_lease(lease, client=client)
