"""Atomic per-hostname cooldowns shared by HTTP workers."""

from __future__ import annotations

import math

from app.core.config import get_settings
from app.workers.coordination import CoordinationUnavailableError
from app.workers.domain_rate_limit import DomainRateRedisClient

_PREFIX = "scraper:domain-cooldown:"

_READ_SCRIPT = """
return redis.call("PTTL", KEYS[1])
"""

_EXTEND_SCRIPT = """
local remaining = redis.call("PTTL", KEYS[1])
if remaining == -1 then
    return -1
end
local requested = tonumber(ARGV[1])
if remaining < requested then
    redis.call("SET", KEYS[1], "1", "PX", ARGV[1])
    return requested
end
return remaining
"""


def _key(hostname: str) -> str:
    normalized = hostname.strip().lower()
    if not normalized or ":" in normalized:
        raise ValueError("hostname must be a non-empty DNS hostname")
    return f"{_PREFIX}{normalized}"


async def _eval_ttl(
    script: str, key: str, *args: str, client: DomainRateRedisClient
) -> int:
    try:
        result = await client.eval(script, 1, key, *args)
        if isinstance(result, bool) or not isinstance(
            result, int | str | bytes | bytearray
        ):
            raise TypeError("invalid Redis cooldown response type")
        remaining = int(result)
        if remaining < 0 and remaining != -2:
            raise ValueError("domain cooldown has no valid TTL")
        return remaining
    except Exception as exc:
        raise CoordinationUnavailableError(
            "could not read or update domain cooldown"
        ) from exc


async def get_domain_cooldown_wait(
    hostname: str, *, client: DomainRateRedisClient
) -> float:
    """Return seconds remaining, or zero when no cooldown exists."""
    remaining = await _eval_ttl(_READ_SCRIPT, _key(hostname), client=client)
    return max(0, remaining) / 1000


async def extend_domain_cooldown(
    hostname: str,
    *,
    client: DomainRateRedisClient,
    retry_after: float | None = None,
) -> float:
    """Start or extend cooldown without shortening an existing TTL."""
    seconds = float(get_settings().domain_cooldown_seconds)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("domain_cooldown_seconds must be positive and finite")
    if retry_after is not None and math.isfinite(retry_after) and retry_after >= 0:
        seconds = max(seconds, retry_after)
    milliseconds = math.ceil(seconds * 1000)
    if milliseconds > 2**63 - 1:
        raise CoordinationUnavailableError("domain cooldown exceeds Redis TTL range")
    remaining = await _eval_ttl(
        _EXTEND_SCRIPT, _key(hostname), str(milliseconds), client=client
    )
    if remaining < 0:
        raise CoordinationUnavailableError("domain cooldown was not established")
    return remaining / 1000
