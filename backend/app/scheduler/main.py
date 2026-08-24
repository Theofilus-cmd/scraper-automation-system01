"""Scheduler process entrypoint.

Phase 0 stub: no scraping targets exist yet, so this process only proves
out the process-lifecycle pattern (structured logging, graceful shutdown,
periodic heartbeat with real dependency checks) that a later phase's
actual due-target polling loop will be built on top of.
"""

import asyncio
import signal

import redis.asyncio as redis

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import check_database_connection

logger = get_logger(__name__)


async def _check_redis(redis_url: str) -> bool:
    client = redis.from_url(redis_url)
    try:
        await client.ping()
        return True
    except Exception:
        return False
    finally:
        await client.aclose()


class Scheduler:
    def __init__(self) -> None:
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        logger.info("shutdown signal received")
        self._stop_event.set()

    async def run(self) -> None:
        settings = get_settings()
        interval = settings.scheduler_heartbeat_interval_seconds
        logger.info("scheduler starting", extra={"heartbeat_interval_seconds": interval})

        while not self._stop_event.is_set():
            db_ok = await check_database_connection()
            redis_ok = await _check_redis(settings.redis_url)
            logger.info("scheduler heartbeat", extra={"db_ok": db_ok, "redis_ok": redis_ok})

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass

        logger.info("scheduler stopped")


async def main() -> None:
    scheduler = Scheduler()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, scheduler.request_stop)

    await scheduler.run()


if __name__ == "__main__":
    asyncio.run(main())
