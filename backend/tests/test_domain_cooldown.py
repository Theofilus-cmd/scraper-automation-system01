"""Cooldown behavior using deterministic Redis responses."""

from __future__ import annotations

import pytest

from app.workers.coordination import CoordinationUnavailableError
from app.workers.domain_cooldown import (
    extend_domain_cooldown,
    get_domain_cooldown_wait,
)


class FakeRedis:
    def __init__(self, result: object = -2, *, unavailable: bool = False) -> None:
        self.result = result
        self.unavailable = unavailable
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []

    async def eval(self, script: str, numkeys: int, *args: str) -> object:
        self.calls.append((script, numkeys, args))
        if self.unavailable:
            raise ConnectionError("Redis unavailable")
        return self.result

    async def aclose(self) -> object:
        return None


@pytest.mark.parametrize(
    ("result", "expected"), [(-2, 0.0), (0, 0.0), (1250, 1.25)]
)
async def test_read_cooldown(result: int, expected: float) -> None:
    client = FakeRedis(result)
    assert await get_domain_cooldown_wait("Example.COM", client=client) == expected
    assert client.calls[0][2] == ("scraper:domain-cooldown:example.com",)


@pytest.mark.parametrize("result", [-1, -3, True, None, "invalid"])
async def test_invalid_redis_response_fails_closed(result: object) -> None:
    with pytest.raises(CoordinationUnavailableError):
        await get_domain_cooldown_wait("example.com", client=FakeRedis(result))


async def test_redis_unavailable_fails_closed() -> None:
    with pytest.raises(CoordinationUnavailableError):
        await get_domain_cooldown_wait(
            "example.com", client=FakeRedis(unavailable=True)
        )


@pytest.mark.parametrize(
    ("retry_after", "milliseconds"),
    [(None, "600000"), (2.0, "600000"), (900.0, "900000")],
)
async def test_extend_uses_configured_or_server_floor(
    retry_after: float | None, milliseconds: str
) -> None:
    client = FakeRedis(900000)
    assert await extend_domain_cooldown(
        "Example.COM", client=client, retry_after=retry_after
    ) == 900.0
    script, numkeys, args = client.calls[0]
    assert numkeys == 1
    assert args == ("scraper:domain-cooldown:example.com", milliseconds)
    assert "remaining < requested" in script


async def test_extend_rejects_missing_ttl() -> None:
    with pytest.raises(CoordinationUnavailableError):
        await extend_domain_cooldown("example.com", client=FakeRedis(-1))


@pytest.mark.integration
async def test_real_redis_cooldown_never_shortens() -> None:
    import asyncio
    import time
    import uuid

    from app.core.config import get_settings
    from app.workers.domain_rate_limit import get_domain_rate_redis_client

    hostname = f"cooldown-test-{uuid.uuid4().hex}.example.test"
    client = get_domain_rate_redis_client()
    floor = float(get_settings().domain_cooldown_seconds)
    longer = max(floor, 1.0) + 300.0
    longest = longer + 300.0
    started = time.monotonic()

    def assert_remaining(actual: float, requested: float) -> None:
        elapsed = time.monotonic() - started
        assert requested - elapsed - 1.0 <= actual <= requested

    try:
        assert await get_domain_cooldown_wait(hostname, client=client) == 0.0

        initial = await extend_domain_cooldown(hostname, client=client)
        assert_remaining(initial, floor)

        extended = await extend_domain_cooldown(
            hostname, client=client, retry_after=longer
        )
        assert_remaining(extended, longer)

        shorter = await extend_domain_cooldown(
            hostname, client=client, retry_after=1.0
        )
        assert_remaining(shorter, longer)

        waits = await asyncio.gather(
            extend_domain_cooldown(
                hostname, client=client, retry_after=longest
            ),
            extend_domain_cooldown(
                hostname, client=client, retry_after=1.0
            ),
        )
        assert all(wait > 0 for wait in waits)

        remaining = await get_domain_cooldown_wait(hostname, client=client)
        assert_remaining(remaining, longest)
    finally:
        await client.aclose()
