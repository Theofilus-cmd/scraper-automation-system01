"""Token-owned, expiring concurrency slots per hostname."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.config import get_settings
from app.workers.coordination import CoordinationUnavailableError
from app.workers.domain_rate_limit import DomainRateRedisClient

_SCRIPT = """
local clock = redis.call("TIME")
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now)
local action = ARGV[1]
local token = ARGV[2]
local result = 0

if action == "acquire" then
    if redis.call("ZCARD", KEYS[1]) < tonumber(ARGV[4]) then
        redis.call("ZADD", KEYS[1], now + tonumber(ARGV[3]), token)
        result = 1
    end
elseif action == "renew" then
    if redis.call("ZSCORE", KEYS[1], token) then
        redis.call("ZADD", KEYS[1], now + tonumber(ARGV[3]), token)
        result = 1
    end
elseif action == "release" then
    result = redis.call("ZREM", KEYS[1], token)
else
    return redis.error_reply("invalid semaphore action")
end

local latest = redis.call("ZRANGE", KEYS[1], -1, -1, "WITHSCORES")
if #latest > 0 then
    redis.call("PEXPIREAT", KEYS[1], math.ceil(tonumber(latest[2])))
end
return result
"""


@dataclass(frozen=True, slots=True)
class DomainSemaphoreLease:
    key: str
    token: str
    ttl_seconds: int


async def _operate(
    action: str,
    lease: DomainSemaphoreLease,
    *,
    client: DomainRateRedisClient,
    limit: int = 0,
) -> bool:
    try:
        result = await client.eval(
            _SCRIPT,
            1,
            lease.key,
            action,
            lease.token,
            str(lease.ttl_seconds * 1000),
            str(limit),
        )
        if isinstance(result, bool) or not isinstance(
            result, int | str | bytes | bytearray
        ):
            raise TypeError("invalid semaphore response")
        value = int(result)
        if value not in (0, 1):
            raise ValueError("invalid semaphore result")
        return value == 1
    except Exception as exc:
        raise CoordinationUnavailableError(
            f"could not {action} domain semaphore"
        ) from exc


async def acquire_domain_semaphore(
    hostname: str, *, client: DomainRateRedisClient
) -> DomainSemaphoreLease | None:
    settings = get_settings()
    normalized = hostname.strip().lower()
    if not normalized or ":" in normalized:
        raise ValueError("hostname must be a non-empty DNS hostname")
    if settings.domain_max_concurrency <= 0:
        raise ValueError("domain_max_concurrency must be positive")
    if settings.task_execution_lease_seconds <= 0:
        raise ValueError("task_execution_lease_seconds must be positive")
    lease = DomainSemaphoreLease(
        key=f"scraper:domain-semaphore:{normalized}",
        token=uuid.uuid4().hex,
        ttl_seconds=settings.task_execution_lease_seconds,
    )
    acquired = await _operate(
        "acquire", lease, client=client, limit=settings.domain_max_concurrency
    )
    return lease if acquired else None


async def renew_domain_semaphore(
    lease: DomainSemaphoreLease, *, client: DomainRateRedisClient
) -> bool:
    return await _operate("renew", lease, client=client)


async def release_domain_semaphore(
    lease: DomainSemaphoreLease, *, client: DomainRateRedisClient
) -> bool:
    return await _operate("release", lease, client=client)
