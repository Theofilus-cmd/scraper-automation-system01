"""Typed wrapper around `redis.asyncio.from_url()` -- the one shared,
narrow place this project's two independent Redis liveness checks
(`app/core/health.py`'s `/readyz`, `app/scheduler/main.py`'s heartbeat
loop) construct a client, so this boundary exists once, not twice.

Acceptance-review fix (real mypy errors, real Docker/Postgres run):
`redis.asyncio.from_url()` resolves as an untyped call under mypy in
this project's pinned redis-py version, wherever it's called directly
(`no-untyped-call`, part of `disallow_untyped_calls` -- this project's
`strict = true`, pyproject.toml). `get_async_redis_client()` below is
the one place that real, untyped call happens; its result is cast to
the small `_RedisPingClient` Protocol below rather than left as a bare
untyped call at either real call site, or fixed by relaxing
`disallow_untyped_calls` project-wide.

The Protocol -- not a `redis.Redis[...]` annotation -- is deliberate:
it covers exactly the two methods either caller actually needs
(`ping`, `aclose`), so this fix does not have to assert anything about
exactly how redis-py's own (possibly generic) `Redis` class is declared
in whatever stub/inline-type shape this project's pinned version ships
-- a claim this sandbox has no way to verify for real (no installed
redis-py, no PyPI access here; see the delivery README). Narrower, and
correct regardless of that detail.
"""

from __future__ import annotations

from typing import Protocol, cast

import redis.asyncio as redis


class _RedisPingClient(Protocol):
    """The one narrow slice of `redis.asyncio.Redis`'s real interface
    either caller uses.
    """

    async def ping(self) -> object: ...
    async def aclose(self) -> object: ...


def get_async_redis_client(url: str) -> _RedisPingClient:
    """See module docstring for the full "why" of the cast below."""
    return cast(_RedisPingClient, redis.from_url(url))  # type: ignore[no-untyped-call]
