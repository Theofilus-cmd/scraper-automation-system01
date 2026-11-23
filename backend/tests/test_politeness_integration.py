"""polite_fetch against real Redis: cooldown, semaphore, rate limit, cleanup."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.scraping.fetcher import FetchError
from app.workers.domain_cooldown import get_domain_cooldown_wait
from app.workers.domain_rate_limit import get_domain_rate_redis_client
from app.workers.domain_semaphore import acquire_domain_semaphore
from app.workers.politeness import DomainDeferredError, polite_fetch

pytestmark = pytest.mark.integration


def _host() -> str:
    return f"polite-{uuid.uuid4().hex}.example.test"


async def _semaphore_count(host: str) -> int:
    client = get_domain_rate_redis_client()
    try:
        return int(
            await client.eval(  # type: ignore[call-overload]
                'return redis.call("ZCARD", KEYS[1])',
                1,
                f"scraper:domain-semaphore:{host}",
            )
        )
    finally:
        await client.aclose()


async def test_success_returns_result_and_releases_slot() -> None:
    host = _host()

    async def ok() -> str:
        assert await _semaphore_count(host) == 1
        return "page"

    assert await polite_fetch(f"https://{host}/p", ok) == "page"
    assert await _semaphore_count(host) == 0


async def test_failure_releases_slot() -> None:
    host = _host()

    async def boom() -> str:
        raise FetchError("network_error", "boom")

    with pytest.raises(FetchError):
        await polite_fetch(f"https://{host}/p", boom)
    assert await _semaphore_count(host) == 0


async def test_cancellation_releases_slot() -> None:
    host = _host()
    started = asyncio.Event()

    async def hang() -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    task = asyncio.create_task(polite_fetch(f"https://{host}/p", hang))
    await started.wait()
    assert await _semaphore_count(host) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _semaphore_count(host) == 0


async def test_503_starts_cooldown_and_next_fetch_is_deferred() -> None:
    host = _host()

    async def throttled() -> str:
        raise FetchError("network_error", "503", retry_after=900, status_code=503)

    with pytest.raises(FetchError):
        await polite_fetch(f"https://{host}/p", throttled)

    client = get_domain_rate_redis_client()
    try:
        assert await get_domain_cooldown_wait(host, client=client) > 800
    finally:
        await client.aclose()

    called = False

    async def should_not_run() -> str:
        nonlocal called
        called = True
        return "x"

    with pytest.raises(DomainDeferredError) as info:
        await polite_fetch(f"https://{host}/p", should_not_run)
    assert info.value.reason == "domain_cooldown"
    assert info.value.delay > 800
    assert called is False
    assert await _semaphore_count(host) == 0


async def test_500_does_not_start_cooldown() -> None:
    host = _host()

    async def broken() -> str:
        raise FetchError("network_error", "500", status_code=500)

    with pytest.raises(FetchError):
        await polite_fetch(f"https://{host}/p", broken)
    client = get_domain_rate_redis_client()
    try:
        assert await get_domain_cooldown_wait(host, client=client) == 0
    finally:
        await client.aclose()


async def test_full_semaphore_defers_without_fetching() -> None:
    host = _host()
    client = get_domain_rate_redis_client()
    try:
        held = [await acquire_domain_semaphore(host, client=client) for _ in range(3)]
        assert all(lease is not None for lease in held)
    finally:
        await client.aclose()

    async def should_not_run() -> str:
        raise AssertionError("fetch must not run")

    with pytest.raises(DomainDeferredError) as info:
        await polite_fetch(f"https://{host}/p", should_not_run)
    assert info.value.reason == "domain_busy"
    assert await _semaphore_count(host) == 3  # others' slots untouched


async def test_rate_limit_spaces_back_to_back_requests() -> None:
    host = _host()
    loop = asyncio.get_running_loop()
    times: list[float] = []

    async def stamp() -> str:
        times.append(loop.time())
        return "ok"

    await polite_fetch(f"https://{host}/a", stamp)
    await polite_fetch(f"https://{host}/b", stamp)
    assert times[1] - times[0] >= 1.5  # domain_rate_limit_seconds default is 2


async def test_fetch_deadline_becomes_timeout_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    host = _host()
    monkeypatch.setattr(get_settings(), "task_execution_lease_seconds", 1)

    async def slow() -> str:
        await asyncio.sleep(5)
        return "late"

    with pytest.raises(FetchError) as info:
        await polite_fetch(f"https://{host}/p", slow)
    assert info.value.reason == "timeout"
    assert await _semaphore_count(host) == 0
