"""Redis-backed best-effort execution coordination for worker attempts.

A task execution lease prevents two live workers from starting the same
durable task simultaneously. It is deliberately not an exactly-once
guarantee: a worker crash, a Redis outage, or a lease that expires while a
slow attempt is still running can still result in redelivery. Durable task
and persistence state remain the source of truth for terminal outcomes.

Each lease has an opaque random owner token. Renewal and release use Lua
compare-and-act scripts, so a stale worker can never extend or delete a
lease acquired later by another worker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, cast

from app.core.config import get_settings
from app.core.redis_client import get_async_redis_client

_TASK_EXECUTION_KEY_PREFIX = "scraper:task-execution:"
_RELEASE_IF_OWNER_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""


class CoordinationUnavailableError(Exception):
    """Redis coordination could not safely acquire an execution lease."""


class TaskExecutionRedisClient(Protocol):
    async def set(
        self, name: str, value: str, *, ex: int, nx: bool
    ) -> object: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object: ...

    async def aclose(self) -> object: ...


@dataclass(frozen=True, slots=True)
class TaskExecutionLease:
    """The tokenized ownership record for one active task execution."""

    key: str
    token: str
    ttl_seconds: int


def _lease_key(task_id: uuid.UUID) -> str:
    return f"{_TASK_EXECUTION_KEY_PREFIX}{task_id}"


def get_task_execution_redis_client() -> TaskExecutionRedisClient:
    """Return the shared Redis client shape needed for one lease lifecycle."""
    settings = get_settings()
    return cast(TaskExecutionRedisClient, get_async_redis_client(settings.redis_url))


async def acquire_task_execution_lease(
    task_id: uuid.UUID, *, client: TaskExecutionRedisClient
) -> TaskExecutionLease | None:
    """Atomically acquire a short-lived lease, or return None if held.

    Raises CoordinationUnavailableError if Redis cannot answer safely. The
    caller owns ``client`` and must close it after release.
    """
    settings = get_settings()
    lease = TaskExecutionLease(
        key=_lease_key(task_id),
        token=str(uuid.uuid4()),
        ttl_seconds=settings.task_execution_lease_seconds,
    )
    try:
        acquired = await client.set(lease.key, lease.token, ex=lease.ttl_seconds, nx=True)
    except Exception as exc:
        raise CoordinationUnavailableError("could not acquire task execution lease") from exc

    return lease if acquired else None


async def release_task_execution_lease(
    lease: TaskExecutionLease, *, client: TaskExecutionRedisClient
) -> bool:
    """Delete a lease only if this attempt still owns its random token."""
    try:
        result = await client.eval(_RELEASE_IF_OWNER_SCRIPT, 1, lease.key, lease.token)
    except Exception as exc:
        raise CoordinationUnavailableError("could not release task execution lease") from exc
    return bool(result)
