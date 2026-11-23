"""Domain semaphore response validation and real Redis ownership."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

import pytest

from app.core.config import get_settings
from app.workers.coordination import CoordinationUnavailableError
from app.workers.domain_rate_limit import get_domain_rate_redis_client
from app.workers.domain_semaphore import (
    acquire_domain_semaphore,
    release_domain_semaphore,
    renew_domain_semaphore,
)


class FakeRedis:
    def __init__(self, result: object) -> None:
        self.result = result

    async def eval(self, script: str, numkeys: int, *args: str) -> object:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> object:
        return None


@pytest.mark.parametrize(
    "result", [True, None, -1, 2, "invalid", ConnectionError("offline")]
)
async def test_invalid_or_unavailable_redis_fails_closed(result: object) -> None:
    with pytest.raises(CoordinationUnavailableError):
        await acquire_domain_semaphore("example.com", client=FakeRedis(result))


@pytest.mark.parametrize("result", [0, 1])
async def test_acquire_response(result: int) -> None:
    lease = await acquire_domain_semaphore("example.com", client=FakeRedis(result))
    assert (lease is not None) == bool(result)


@pytest.mark.integration
async def test_real_redis_capacity_and_token_ownership() -> None:
    hostname = f"semaphore-test-{uuid.uuid4().hex}.example.test"
    client = get_domain_rate_redis_client()
    limit = get_settings().domain_max_concurrency
    leases = []
    try:
        results = await asyncio.gather(
            *(
                acquire_domain_semaphore(hostname, client=client)
                for _ in range(limit + 2)
            )
        )
        leases = [lease for lease in results if lease is not None]
        assert len(leases) == limit
        assert len({lease.token for lease in leases}) == limit

        owner = leases[0]
        stranger = replace(owner, token=uuid.uuid4().hex)
        assert not await release_domain_semaphore(stranger, client=client)
        assert not await renew_domain_semaphore(stranger, client=client)
        assert await acquire_domain_semaphore(hostname, client=client) is None
        assert await renew_domain_semaphore(owner, client=client)

        assert await release_domain_semaphore(owner, client=client)
        assert not await release_domain_semaphore(owner, client=client)
        replacement = await acquire_domain_semaphore(hostname, client=client)
        assert replacement is not None
        leases.append(replacement)

        assert not await renew_domain_semaphore(owner, client=client)
        assert not await release_domain_semaphore(owner, client=client)
        assert await acquire_domain_semaphore(hostname, client=client) is None
    finally:
        try:
            for lease in leases:
                await release_domain_semaphore(lease, client=client)
        finally:
            await client.aclose()


@pytest.mark.integration
@pytest.mark.parametrize("expiry_mode", ["member", "key"])
async def test_real_redis_expired_owner_cannot_return(expiry_mode: str) -> None:
    hostname = f"semaphore-expiry-{uuid.uuid4().hex}.example.test"
    client = get_domain_rate_redis_client()
    leases = []
    try:
        owner = await acquire_domain_semaphore(hostname, client=client)
        assert owner is not None
        leases.append(owner)

        if expiry_mode == "member":
            await client.eval(
                'return redis.call("ZADD", KEYS[1], 0, ARGV[1])',
                1,
                owner.key,
                owner.token,
            )
        else:
            await client.eval(
                'return redis.call("PEXPIREAT", KEYS[1], 1)',
                1,
                owner.key,
            )

        assert not await renew_domain_semaphore(owner, client=client)

        replacement = await acquire_domain_semaphore(hostname, client=client)
        assert replacement is not None
        leases.append(replacement)
        assert replacement.token != owner.token

        assert not await release_domain_semaphore(owner, client=client)
        assert not await renew_domain_semaphore(owner, client=client)
        assert await renew_domain_semaphore(replacement, client=client)
    finally:
        try:
            for lease in leases:
                await release_domain_semaphore(lease, client=client)
        finally:
            await client.aclose()
