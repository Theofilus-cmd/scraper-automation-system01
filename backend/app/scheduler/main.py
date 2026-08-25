"""Scheduler process entrypoint.

Phase 0/1 shipped a heartbeat-only stub (kept below, unchanged: it still
proves the process-lifecycle pattern -- structured logging, graceful
shutdown, periodic dependency checks). Phase 2 (doc 18 §4, §7.5) adds this
process's actual job: three more independent, concurrently-running loops
sharing the same graceful-shutdown event --

  - claim loop   (§4.1): the atomic due-schedule claim-and-dispatch, every
    `scheduler_claim_interval_seconds` (default 10s).
  - reconciliation loop (doc 07 §5, reused directly per doc 18 §1.1): the
    stuck-task/orphaned-run sweep, every `reconciliation_interval_seconds`
    (default 120s, doc 07 §5's own shipped default).
  - retention loop (§7.5): the history/task/run purge, every
    `retention_purge_interval_seconds` (default hourly -- a purge sweep
    doesn't need the claim loop's ~10s cadence).

All four loops (heartbeat included) run via `asyncio.gather` inside one
process, one event loop, for the process's whole life -- this is exactly
the "one loop, never repeated" shape `app/workers/async_bridge.py`'s
docstring already notes this process doesn't need the Celery-worker
loop-lifecycle fix for.
"""

import asyncio
import signal
from datetime import timedelta

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis_client import get_async_redis_client
from app.db.models.retention import purge_expired_history
from app.db.models.runs_repository import claim_due_schedules, reconciliation_sweep
from app.db.session import check_database_connection

logger = get_logger(__name__)


async def _check_redis(redis_url: str) -> bool:
    client = get_async_redis_client(redis_url)
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

    async def _wait_or_stop(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except TimeoutError:
            pass

    async def _heartbeat_loop(self, *, interval: int) -> None:
        """Unchanged from Phase 0/1."""
        while not self._stop_event.is_set():
            db_ok = await check_database_connection()
            redis_ok = await _check_redis(get_settings().redis_url)
            logger.info("scheduler heartbeat", extra={"db_ok": db_ok, "redis_ok": redis_ok})
            await self._wait_or_stop(interval)

    async def _claim_loop(self, *, interval: int, batch_size: int) -> None:
        """doc 18 §4.1. One failed cycle (e.g. a transient DB hiccup) must
        not crash the whole scheduler process -- caught and logged, next
        cycle tries again `interval` seconds later.
        """
        while not self._stop_event.is_set():
            try:
                await claim_due_schedules(batch_size=batch_size)
            except Exception:
                logger.exception("scheduler claim cycle failed")
            await self._wait_or_stop(interval)

    async def _reconciliation_loop(self, *, interval: int, stuck_after_minutes: int) -> None:
        """doc 07 §5, reused directly (doc 18 §1.1), plus its documented
        orphaned-run extension (see runs_repository.reconciliation_sweep's
        docstring)."""
        stuck_after = timedelta(minutes=stuck_after_minutes)
        while not self._stop_event.is_set():
            try:
                await reconciliation_sweep(stuck_after=stuck_after)
            except Exception:
                logger.exception("reconciliation sweep failed")
            await self._wait_or_stop(interval)

    async def _retention_loop(self, *, interval: int, retention_days: int) -> None:
        """doc 18 §7.5."""
        while not self._stop_event.is_set():
            try:
                await purge_expired_history(retention_days=retention_days)
            except Exception:
                logger.exception("retention purge failed")
            await self._wait_or_stop(interval)

    async def run(self) -> None:
        settings = get_settings()
        logger.info(
            "scheduler starting",
            extra={
                "heartbeat_interval_seconds": settings.scheduler_heartbeat_interval_seconds,
                "claim_interval_seconds": settings.scheduler_claim_interval_seconds,
                "scheduler_claim_batch_size": settings.scheduler_claim_batch_size,
                "reconciliation_interval_seconds": settings.reconciliation_interval_seconds,
                "retention_purge_interval_seconds": settings.retention_purge_interval_seconds,
                "history_retention_days": settings.history_retention_days,
            },
        )

        await asyncio.gather(
            self._heartbeat_loop(interval=settings.scheduler_heartbeat_interval_seconds),
            self._claim_loop(
                interval=settings.scheduler_claim_interval_seconds,
                batch_size=settings.scheduler_claim_batch_size,
            ),
            self._reconciliation_loop(
                interval=settings.reconciliation_interval_seconds,
                stuck_after_minutes=settings.reconciliation_stuck_task_timeout_minutes,
            ),
            self._retention_loop(
                interval=settings.retention_purge_interval_seconds,
                retention_days=settings.history_retention_days,
            ),
        )

        logger.info("scheduler stopped")


async def main() -> None:
    scheduler = Scheduler()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, scheduler.request_stop)

    await scheduler.run()


if __name__ == "__main__":
    asyncio.run(main())
