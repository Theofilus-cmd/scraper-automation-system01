"""Fixed-window request counter in Redis, used to throttle auth endpoints."""

from __future__ import annotations

import math
from typing import Protocol, cast

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis_client import get_async_redis_client

logger = get_logger(__name__)

_PREFIX = "scraper:rate-limit:"
_SCRIPT = """
local count = redis.call("INCR", KEYS[1])
if count == 1 or redis.call("PTTL", KEYS[1]) < 0 then
    redis.call("PEXPIRE", KEYS[1], ARGV[1])
end
return {count, redis.call("PTTL", KEYS[1])}
"""


class _EvalClient(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object: ...

    async def aclose(self) -> object: ...


async def enforce_rate_limit(scope: str, identifier: str) -> None:
    """Count one hit for (scope, identifier); raise 429 once over the limit."""
    settings = get_settings()
    if not settings.auth_rate_limit_enabled:
        return

    window_ms = settings.auth_rate_limit_window_seconds * 1000
    client = cast(_EvalClient, get_async_redis_client(settings.redis_url))
    try:
        result = await client.eval(_SCRIPT, 1, f"{_PREFIX}{scope}:{identifier}", str(window_ms))
        if not isinstance(result, list) or len(result) != 2:
            raise TypeError("invalid rate limit response")
        count, ttl_ms = int(result[0]), int(result[1])
    except Exception:
        logger.warning("rate limiter unavailable; allowing request", exc_info=True)
        return
    finally:
        try:
            await client.aclose()
        except Exception:
            logger.warning("rate limiter client close failed", exc_info=True)

    if count > settings.auth_rate_limit_attempts:
        retry_after = max(1, math.ceil(ttl_ms / 1000))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
