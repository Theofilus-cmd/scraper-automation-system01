"""Atomic Redis rate slots shared by HTTP workers for each hostname."""

from __future__ import annotations

from typing import Protocol, cast

from app.core.config import get_settings
from app.core.redis_client import get_async_redis_client
from app.workers.coordination import CoordinationUnavailableError

_DOMAIN_RATE_KEY_PREFIX = "scraper:domain-rate:"

_ACQUIRE_RATE_SLOT_SCRIPT = """
if redis.call("SET", KEYS[1], "1", "NX", "PX", ARGV[1]) then
    return 0
end
local remaining = redis.call("PTTL", KEYS[1])
if remaining == -2 then
    if redis.call("SET", KEYS[1], "1", "NX", "PX", ARGV[1]) then
        return 0
    end
    remaining = redis.call("PTTL", KEYS[1])
end
return remaining
"""


class DomainRateRedisClient(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object: ...

    async def aclose(self) -> object: ...


def get_domain_rate_redis_client() -> DomainRateRedisClient:
    """Create the Redis client used for one rate-slot request."""
    return cast(DomainRateRedisClient, get_async_redis_client(get_settings().redis_url))


async def acquire_domain_rate_slot(
    hostname: str, *, client: DomainRateRedisClient
) -> float:
    """Return 0 if admitted, otherwise the remaining wait in seconds.

    The caller owns and closes the client. Redis failures deny permission
    rather than allowing an unthrottled HTTP request.
    """
    normalized = hostname.strip().lower()
    if not normalized or ":" in normalized:
        raise ValueError("hostname must be a non-empty DNS hostname")

    interval_ms = get_settings().domain_rate_limit_seconds * 1000
    if interval_ms <= 0:
        raise ValueError("domain_rate_limit_seconds must be positive")

    key = f"{_DOMAIN_RATE_KEY_PREFIX}{normalized}"
    try:
        remaining_ms = int(
            await client.eval(_ACQUIRE_RATE_SLOT_SCRIPT, 1, key, str(interval_ms))
        )
    except Exception as exc:
        raise CoordinationUnavailableError("could not acquire domain rate slot") from exc

    if remaining_ms < 0:
        raise CoordinationUnavailableError("domain rate slot has no valid TTL")
    return remaining_ms / 1000
