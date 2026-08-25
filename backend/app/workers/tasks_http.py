"""HTTP-fetch queue task: `scrape_source_url` -- the real
fetch->parse->normalize->validate->persist pipeline, dispatched by
`app/db/models/runs_repository.py`'s phase-C dispatch (every trigger path:
scheduler claim, `POST /sources/{id}/runs`, and the legacy
`POST /api/v1/scrapes` alias all funnel through the same run/task creation
code, so they all end up here the same way).

Phase 2 change from Phase 1, flagged plainly: this task used to take a raw
`source_url` string and was the request/response contract's own source of
truth (its Celery return value, read back via `AsyncResult`, WAS the HTTP
response body). Neither is true anymore. It now takes a `tasks.id` (the
task row already created by the caller before dispatch, doc 18 §4.1) and
drives that row's (and its parent run's) status through the doc 18 §2.3
state machine; the durable `tasks`/`runs` tables are the single source of
truth an HTTP response is built from (`app/api/v1/scrapes.py`,
`app/api/v1/runs.py` both query fresh rather than trusting this task's
return value) -- doc 18 §6.6's whole point in retiring the old
Redis-internals `_task_exists` hack.

Celery invokes tasks synchronously; there is no running event loop inside
a worker process, so every async call this task needs (fetch, DB) is
bridged via `app.workers.async_bridge.run_async()` -- unchanged from
Phase 1, see that module's docstring.

`ping` (doc 04 §1's original worker/queue smoke test) is kept below,
untouched, per every prior phase's convention.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from celery import Task as CeleryTask
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models.lifecycle import TASK_TRANSIENT_REASONS, Run
from app.db.models.lifecycle import Task as TaskRow
from app.db.models.repository import upsert_scrape_result
from app.db.models.scraping import Source
from app.db.session import get_session
from app.domain.validation import validate
from app.scraping.adapters import registry
from app.scraping.fetcher import FetchError
from app.scraping.normalize import normalize
from app.scraping.types import ExtractionSchema, FetchContext
from app.workers.async_bridge import run_async
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

# doc 05's tasks.error_reason vocabulary -> (HTTP status, doc 06 §1 error
# code), reused unchanged by both app/api/v1/scrapes.py (the legacy alias,
# doc 18 §6.6) and, for the same reasons, the new run/task endpoints.
# "source_archived" (new, Phase 2 -- the race doc 18 §3.2 documents) maps
# to 409 SOURCE_ARCHIVED for consistency with the new API's own
# archived-source responses (doc 18 §6.4) -- doc 18 §6.6 doesn't spell this
# specific legacy-endpoint edge case out explicitly; this is a small,
# flagged, reasonable extrapolation of its stated vocabulary-reuse
# principle, not a literal transcription.
ERROR_REASON_TO_RESPONSE: dict[str, tuple[int, str]] = {
    "missing_required_field": (422, "MISSING_REQUIRED_FIELD"),
    "unsupported_adapter": (422, "UNSUPPORTED_ADAPTER"),
    "parse_error": (422, "NO_PRODUCT_FOUND"),
    "timeout": (502, "FETCH_FAILED"),
    "network_error": (502, "FETCH_FAILED"),
    "dns_or_ssrf_blocked": (502, "FETCH_FAILED"),
    "source_archived": (409, "SOURCE_ARCHIVED"),
}


class TransientScrapeError(Exception):
    """doc 18 §5.1/§5.2: raised for the two retryable `error_reason`s
    (`timeout`, `network_error`). Caught by `scrape_source_url` itself
    (not via `autoretry_for` -- that mechanism can't express the
    `Retry-After`-aware custom countdown doc 18 §5.3 requires), which then
    drives `self.retry(...)`.
    """

    def __init__(self, reason: str, message: str, *, retry_after: float | None = None) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(message)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _TaskContext:
    task_id: uuid.UUID
    run_id: uuid.UUID
    source_id: uuid.UUID
    source_url: str
    source_status: str


async def _load_task_context(task_id: uuid.UUID) -> _TaskContext | None:
    async with get_session() as session:
        row = (
            await session.execute(
                select(TaskRow.run_id, TaskRow.source_id, Source.url, Source.status)
                .join(Source, Source.id == TaskRow.source_id)
                .where(TaskRow.id == task_id)
            )
        ).first()
    if row is None:
        return None
    return _TaskContext(
        task_id=task_id,
        run_id=row.run_id,
        source_id=row.source_id,
        source_url=row.url,
        source_status=row.status,
    )


async def _mark_task_started(task_id: uuid.UUID, *, attempt: int) -> None:
    async with get_session() as session:
        task = (await session.execute(select(TaskRow).where(TaskRow.id == task_id))).scalar_one()
        task.status = "in_progress"
        task.attempt_count = attempt
        if task.started_at is None:
            task.started_at = _utcnow()
        await session.commit()


async def _mark_task_retrying(task_id: uuid.UUID, *, reason: str, message: str) -> None:
    async with get_session() as session:
        task = (await session.execute(select(TaskRow).where(TaskRow.id == task_id))).scalar_one()
        task.status = "retrying"
        task.error_reason = reason
        task.error_detail = {"message": message}
        await session.commit()


async def _finalize_task(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    task_status: str,
    run_status: str,
    error_reason: str | None = None,
    error_detail: dict[str, Any] | None = None,
) -> None:
    """doc 18 §3.2: the run's ONE task reaching a terminal state is what
    finalizes the run itself (`succeeded -> completed`, `failed`/
    `dead_letter -> failed`) -- the simplified, single-task version of doc
    07 §1's general run-status formula, matching doc 18 §2.3's cardinality
    note that every run has exactly one task today. `succeeded_tasks`/
    `failed_tasks` are incremented (not flatly set to 1) so this keeps
    working unmodified if that cardinality ever changes.
    """
    now = _utcnow()
    async with get_session() as session:
        task = (await session.execute(select(TaskRow).where(TaskRow.id == task_id))).scalar_one()
        task.status = task_status
        task.finished_at = now
        if error_reason is not None:
            task.error_reason = error_reason
            task.error_detail = error_detail

        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        run.status = run_status
        run.finished_at = now
        if run_status == "completed":
            run.succeeded_tasks += 1
        elif run_status == "failed":
            run.failed_tasks += 1
        await session.commit()


async def _finalize_task_success(task_id: uuid.UUID, run_id: uuid.UUID) -> None:
    await _finalize_task(task_id, run_id, task_status="succeeded", run_status="completed")


async def _finalize_task_failure(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    reason: str,
    message: str,
    extra_detail: dict[str, Any] | None = None,
) -> None:
    """`extra_detail` (added alongside the API layer, commit 4): merged
    into `error_detail` beyond the plain `{"message": ...}` shape --
    `missing_required_field`'s caller below is the one case that needs
    this today (`validation_errors`), so that the legacy alias
    (app/api/v1/scrapes.py, doc 18 §6.6) can surface it from durable state
    the same way Phase 1's original response did, instead of only from the
    Celery return value this design deliberately stops trusting.
    """
    detail: dict[str, Any] = {"message": message}
    if extra_detail:
        detail.update(extra_detail)
    await _finalize_task(
        task_id,
        run_id,
        task_status="failed",
        run_status="failed",
        error_reason=reason,
        error_detail=detail,
    )


async def _finalize_task_dead_letter(
    task_id: uuid.UUID, run_id: uuid.UUID, *, reason: str, message: str
) -> None:
    await _finalize_task(
        task_id,
        run_id,
        task_status="dead_letter",
        run_status="failed",
        error_reason=reason,
        error_detail={"message": message},
    )


@celery_app.task(name="app.workers.tasks_http.ping")
def ping() -> dict[str, str]:
    logger.info("ping task executed", extra={"queue": "http"})
    return {"queue": "http", "status": "pong"}


async def _scrape_source_url_for_task(task_id: uuid.UUID, *, attempt: int) -> dict[str, Any]:
    context = await _load_task_context(task_id)
    if context is None:
        # Should not happen: the caller always creates the tasks row before
        # ever dispatching (doc 18 §4.1's phase B, strictly before phase C).
        logger.error("scrape task row not found", extra={"task_id": str(task_id)})
        return {"status": "failed", "error_reason": "task_not_found"}

    await _mark_task_started(task_id, attempt=attempt)

    if context.source_status == "archived":
        # doc 18 §3.2's documented race: archived after this task was
        # created but before it started executing.
        await _finalize_task_failure(
            task_id,
            context.run_id,
            reason="source_archived",
            message="source was archived before this task ran",
        )
        return {"status": "failed", "error_reason": "source_archived"}

    adapter = registry.detect(context.source_url)
    if adapter is None:
        # Defensive: doc 18 §6.1's create-time check already rejects an
        # unsupported URL before a source (and so a run/task) can ever
        # exist for it. Reachable here only via a lower-level test or a
        # future adapter-removal edge case.
        await _finalize_task_failure(
            task_id, context.run_id, reason="unsupported_adapter", message="no adapter matched"
        )
        return {"status": "failed", "error_reason": "unsupported_adapter"}

    try:
        page = await adapter.fetch(context.source_url, FetchContext())
    except FetchError as exc:
        if exc.reason in TASK_TRANSIENT_REASONS:
            raise TransientScrapeError(exc.reason, str(exc), retry_after=exc.retry_after) from exc
        await _finalize_task_failure(task_id, context.run_id, reason=exc.reason, message=str(exc))
        return {"status": "failed", "error_reason": exc.reason}

    raw_records = adapter.parse(page, ExtractionSchema.all_fields())
    if not raw_records:
        await _finalize_task_failure(
            task_id, context.run_id, reason="parse_error", message="no product found"
        )
        return {"status": "failed", "error_reason": "parse_error"}

    # Phase 1 (doc 17): one URL == one product page, unchanged in Phase 2
    # (doc 18 §9's non-goals: no multi-product-per-source fan-out).
    record = normalize(raw_records[0], context.source_url)
    validation = validate(record)

    result = await upsert_scrape_result(
        source_id=context.source_id,
        run_id=context.run_id,
        task_id=task_id,
        record=record,
        validation=validation,
    )

    if not result.is_valid:
        await _finalize_task_failure(
            task_id,
            context.run_id,
            reason="missing_required_field",
            message="required field(s) missing or invalid",
            extra_detail={"validation_errors": result.validation_errors},
        )
        return {
            "status": "failed",
            "error_reason": "missing_required_field",
            "validation_errors": result.validation_errors,
        }

    await _finalize_task_success(task_id, context.run_id)
    return {
        "status": "completed",
        "product_id": str(result.product_id),
        "created": result.created,
        "history_appended": result.history_appended,
    }


@celery_app.task(
    name="app.workers.tasks_http.scrape_source_url",
    bind=True,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=2,
)
def scrape_source_url(self: CeleryTask, task_id: str) -> dict[str, Any]:
    """doc 18 §5.2: `max_retries=2` (2 retries + the first attempt =
    `max_attempts=3`, doc 07 §3). `retry_backoff`/`retry_backoff_max`/
    `retry_jitter` govern whatever `self.retry()` call below doesn't pass
    an explicit `countdown` -- i.e. every retry except one honoring a real
    `Retry-After` header (doc 18 §5.3).
    """
    attempt = self.request.retries + 1
    task_uuid = uuid.UUID(task_id)
    logger.info(
        "scrape task started", extra={"task_id": task_id, "attempt": attempt, "queue": "http"}
    )

    try:
        result: dict[str, Any] = run_async(_scrape_source_url_for_task(task_uuid, attempt=attempt))
    except TransientScrapeError as exc:
        run_async(_mark_task_retrying(task_uuid, reason=exc.reason, message=str(exc)))
        try:
            raise self.retry(exc=exc, countdown=exc.retry_after)
        except MaxRetriesExceededError:
            context = run_async(_load_task_context(task_uuid))
            if context is not None:
                run_async(
                    _finalize_task_dead_letter(
                        task_uuid, context.run_id, reason=exc.reason, message=str(exc)
                    )
                )
            logger.warning(
                "scrape task dead-lettered after exhausting retries",
                extra={"task_id": task_id, "reason": exc.reason, "attempt": attempt},
            )
            return {"status": "failed", "error_reason": exc.reason, "dead_letter": True}

    logger.info("scrape task finished", extra={"task_id": task_id, "status": result.get("status")})
    return result
