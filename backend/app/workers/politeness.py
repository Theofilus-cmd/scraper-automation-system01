"""Per-domain politeness gate wrapped around one HTTP fetch (doc 19).

Order of checks, all fail-closed (a Redis failure raises
`CoordinationUnavailableError` instead of letting an unthrottled request
through):

1. cooldown  -- host recently answered 429/503: do not fetch, defer.
2. semaphore -- at most `domain_max_concurrency` in-flight fetches per host.
3. rate slot -- at most one request per `domain_rate_limit_seconds` per host.

The semaphore slot is always released in `finally`, covering success,
failure, timeout and task cancellation (lease loss). Politeness is keyed on
the source URL's hostname; redirect hops to other hosts are still SSRF
checked by the fetcher but not individually throttled.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar
from urllib.parse import urlparse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.scraping.fetcher import FetchError
from app.workers.coordination import CoordinationUnavailableError
from app.workers.domain_cooldown import extend_domain_cooldown, get_domain_cooldown_wait
from app.workers.domain_rate_limit import (
    DomainRateRedisClient,
    acquire_domain_rate_slot,
    get_domain_rate_redis_client,
)
from app.workers.domain_semaphore import (
    acquire_domain_semaphore,
    release_domain_semaphore,
)

logger = get_logger(__name__)

_T = TypeVar("_T")

# A rate wait up to this long is slept inline while holding the semaphore
# slot; anything longer is deferred so the slot is not held idle.
MAX_INLINE_RATE_WAIT_SECONDS = 5.0
COOLDOWN_STATUS_CODES = (429, 503)


class DomainDeferredError(Exception):
    """Politeness forbids fetching now; retry after `delay` seconds.

    Not a fetch failure: it must not consume a durable attempt.
    """

    def __init__(self, reason: str, delay: float) -> None:
        self.reason = reason
        self.delay = delay
        super().__init__(f"{reason}: retry in {delay:.1f}s")


def hostname_for_url(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower()
    # IPv6 literals contain ":"; the Redis key helpers reject that.
    return hostname.replace(":", "_")


def _jitter(delay: float) -> float:
    return delay + random.uniform(0.0, 1.0)


async def polite_fetch(
    url: str,
    do_fetch: Callable[[], Awaitable[_T]],
    *,
    client: DomainRateRedisClient | None = None,
) -> _T:
    """Run `do_fetch` under cooldown, semaphore and rate-limit control."""
    hostname = hostname_for_url(url)
    if not hostname:
        raise FetchError("dns_or_ssrf_blocked", "url has no hostname")

    settings = get_settings()
    owns_client = client is None
    redis = client if client is not None else get_domain_rate_redis_client()
    slot = None
    try:
        cooldown = await get_domain_cooldown_wait(hostname, client=redis)
        if cooldown > 0:
            raise DomainDeferredError("domain_cooldown", _jitter(cooldown))

        slot = await acquire_domain_semaphore(hostname, client=redis)
        if slot is None:
            raise DomainDeferredError(
                "domain_busy", _jitter(float(settings.domain_rate_limit_seconds))
            )

        wait = await acquire_domain_rate_slot(hostname, client=redis)
        if wait > MAX_INLINE_RATE_WAIT_SECONDS:
            raise DomainDeferredError("domain_rate_limited", _jitter(wait))
        if wait > 0:
            await asyncio.sleep(wait)
            wait = await acquire_domain_rate_slot(hostname, client=redis)
            if wait > 0:
                raise DomainDeferredError("domain_rate_limited", _jitter(wait))

        # The slot lives `task_execution_lease_seconds`; never let a fetch
        # outlive it, so an expired slot can't hide a still-running request.
        deadline = settings.task_execution_lease_seconds * 0.75
        try:
            return await asyncio.wait_for(do_fetch(), timeout=deadline)
        except TimeoutError as exc:
            raise FetchError("timeout", f"fetch exceeded {deadline:.0f}s deadline") from exc
        except FetchError as exc:
            if exc.status_code in COOLDOWN_STATUS_CODES:
                await _start_cooldown(hostname, exc, redis)
            raise
    finally:
        try:
            if slot is not None:
                await release_domain_semaphore(slot, client=redis)
        except CoordinationUnavailableError:
            logger.exception(
                "domain semaphore release failed", extra={"hostname": hostname}
            )
        if owns_client:
            try:
                await redis.aclose()
            except Exception:
                logger.exception("domain redis client close failed")


async def _start_cooldown(
    hostname: str, exc: FetchError, client: DomainRateRedisClient
) -> None:
    try:
        remaining = await extend_domain_cooldown(
            hostname, client=client, retry_after=exc.retry_after
        )
        logger.warning(
            "domain cooldown started",
            extra={"hostname": hostname, "seconds": remaining, "status": exc.status_code},
        )
    except CoordinationUnavailableError:
        # The original FetchError still propagates; the next attempt's
        # cooldown check fails closed if Redis is still down.
        logger.exception("could not start domain cooldown", extra={"hostname": hostname})
