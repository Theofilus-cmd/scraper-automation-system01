"""Unit and Redis integration tests for atomic per-hostname rate slots."""

from __future__ import annotations

import pytest

from app.workers.coordination import CoordinationUnavailableError
from app.workers.domain_rate_limit import acquire_domain_rate_slot


class FakeDomainRedisClient:
    def __init__(
        self, *, results: list[object] | None = None, error: Exception | None = None
    ) -> None:
        self.results = list(results or [])
        self.error = error
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []

    async def eval(self, script: str, numkeys: int, *args: str) -> object:
        self.calls.append((script, numkeys, args))
        if self.error is not None:
            raise self.error
        if not self.results:
            raise AssertionError("unexpected Redis EVAL call")
        return self.results.pop(0)

    async def aclose(self) -> object:
        return None


async def test_first_domain_request_gets_slot_and_uses_atomic_ttl() -> None:
    client = FakeDomainRedisClient(results=[0])

    assert await acquire_domain_rate_slot("Example.COM", client=client) == 0.0

    script, numkeys, args = client.calls[0]
    assert numkeys == 1
    assert args == ("scraper:domain-rate:example.com", "2000")
    assert 'redis.call("SET", KEYS[1], "1", "NX", "PX", ARGV[1])' in script
    assert 'redis.call("PTTL", KEYS[1])' in script


async def test_busy_domain_returns_remaining_wait_without_new_slot() -> None:
    client = FakeDomainRedisClient(results=[1250])

    assert await acquire_domain_rate_slot("example.com", client=client) == 1.25
    assert len(client.calls) == 1


async def test_different_hostnames_use_different_keys() -> None:
    client = FakeDomainRedisClient(results=[0, 0])

    await acquire_domain_rate_slot("a.example.com", client=client)
    await acquire_domain_rate_slot("b.example.com", client=client)

    assert client.calls[0][2][0] != client.calls[1][2][0]


async def test_redis_failure_fails_closed() -> None:
    client = FakeDomainRedisClient(error=ConnectionError("redis unavailable"))

    with pytest.raises(CoordinationUnavailableError, match="domain rate slot"):
        await acquire_domain_rate_slot("example.com", client=client)


async def test_missing_ttl_fails_closed() -> None:
    client = FakeDomainRedisClient(results=[-1])

    with pytest.raises(CoordinationUnavailableError, match="valid TTL"):
        await acquire_domain_rate_slot("example.com", client=client)


@pytest.mark.integration
async def test_concurrent_requests_for_same_hostname_get_only_one_slot() -> None:
    """Real Redis must serialize simultaneous requests across callers."""
    import asyncio
    import uuid

    from app.workers.domain_rate_limit import get_domain_rate_redis_client

    hostname = f"rate-test-{uuid.uuid4().hex}.example.test"
    client = get_domain_rate_redis_client()
    try:
        waits = await asyncio.gather(
            acquire_domain_rate_slot(hostname, client=client),
            acquire_domain_rate_slot(hostname, client=client),
        )
    finally:
        await client.aclose()

    assert sum(wait == 0 for wait in waits) == 1
    assert sum(wait > 0 for wait in waits) == 1
