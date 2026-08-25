"""`POST /api/v1/scrapes`, `GET /api/v1/scrapes/{task_id}` -- doc 18 §6.6:
kept as compatibility aliases over the new `runs`/`tasks` pipeline, not
replaced and not deprecated-and-scheduled-for-removal. Every observable
request/response behavior a Phase 1 caller already depends on is preserved
exactly; all the actual work now routes through the durable pipeline
underneath (find-or-create the source, trigger-or-attach a run, poll
`tasks.status` in Postgres directly instead of Celery's ephemeral Redis
result backend).

Phase 2 change from Phase 1, flagged plainly: this module used to dispatch
`scrape_source_url.delay(...)` itself and read the Celery `AsyncResult`
directly. It does neither now -- run/task creation and Celery dispatch both
happen inside `app.db.models.runs_repository.create_manual_run()` (shared
with every other trigger path), and every response here is built strictly
from durable Postgres state, never from a Celery return value, so a
`GET .../{task_id}` still works correctly even long after that task's
Celery result has expired from Redis.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.serializers import current_observation_to_dict, product_to_dict, source_to_dict
from app.core.errors import ApiError
from app.core.logging import get_logger
from app.db.models.lifecycle import TASK_TERMINAL_STATUSES
from app.db.models.lifecycle import Task as TaskRow
from app.db.models.products_repository import (
    get_current_observation,
    most_recent_product_for_source,
    task_created_new_product,
)
from app.db.models.runs_repository import create_manual_run, first_task_id_for_run
from app.db.models.sources_repository import create_or_get_source, get_source
from app.db.session import get_session
from app.domain.errors import UnsupportedAdapterError
from app.scraping.adapters import registry
from app.workers.tasks_http import ERROR_REASON_TO_RESPONSE

logger = get_logger(__name__)

router = APIRouter(prefix="/scrapes", tags=["scrapes"])

# doc 18 §6.6 point 5: same interval, same total budget as Phase 1 -- only
# the polling target changed (tasks.status in Postgres, not
# AsyncResult.ready() over Redis).
POLL_INTERVAL_SECONDS = 0.5
MAX_WAIT_SECONDS = 35.0

DEPRECATION_HEADER = "Deprecation"


class LegacyScrapesDeprecationMiddleware(BaseHTTPMiddleware):
    """doc 18 §6.6 point 7: every response from the legacy endpoints below
    carries `Deprecation: true`, including error responses -- also logged
    at INFO so real usage becomes observable. Stamping the header inside
    each handler (mutating the injected `Response`) only covers the
    normal-return path: a handler that raises `ApiError` or a domain error
    instead produces a wholly separate `JSONResponse` built by the
    exception handlers in app/core/errors.py / error_mapping.py, whose
    headers that `Response` object never touches. A thin middleware --
    the same shape `RequestIdMiddleware` (app/core/errors.py) already uses
    for the identical problem -- sees the final response on every path,
    success or error alike, so it's the correct place for this, not a
    per-handler call.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/scrapes"):
            response.headers[DEPRECATION_HEADER] = "true"
            logger.info(
                "legacy scrapes endpoint used",
                extra={"path": request.url.path, "method": request.method},
            )
        return response


class ScrapeRequest(BaseModel):
    source_url: str = Field(min_length=1)


async def _load_task(task_id: uuid.UUID) -> TaskRow | None:
    async with get_session() as session:
        result: TaskRow | None = (
            await session.execute(select(TaskRow).where(TaskRow.id == task_id))
        ).scalar_one_or_none()
        return result


async def _build_terminal_response(task: TaskRow) -> dict[str, Any]:
    """Only ever called once `task.status` is known terminal (`succeeded`/
    `failed`/`dead_letter`, doc 18 §2.3). Success is assembled from the
    task's `source_id` and "the resulting `current_observations` row"
    (doc 18 §6.6 point 6's own words) -- current state, not a
    reconstruction of exactly what this one task saw, matching that
    literal instruction; the honest characteristic that follows from it is
    a `GET` on an old `task_id` after a newer scrape of the same source has
    since completed reflects the newer values, consistent with "the
    database is the single source of truth" (repository.py's own framing),
    not a bug.

    doc 18 §6.6 describes assembling this from "the task's ... product_id"
    -- `tasks`/`runs` carry no such column (not part of migration 0003's
    schema, doc 18 §2.3), so this resolves via `source_id` (which `tasks`
    does carry) plus the one-product-per-source-in-practice invariant doc
    18 §9 keeps in place instead, which is equivalent given that invariant
    and avoids a schema change this late for a pure response-shape
    convenience -- flagged here and in the delivery report, not silent.
    """
    if task.status == "succeeded":
        async with get_session() as session:
            source = await get_source(session, task.source_id)
            product = await most_recent_product_for_source(session, task.source_id)
            observation = (
                await get_current_observation(session, product.id)
                if product is not None
                else None
            )
            created = await task_created_new_product(session, task.id)
        if source is None or product is None:
            # A succeeded task's source_id/product are guaranteed to exist
            # by the RESTRICT FKs and upsert_scrape_result's own contract
            # (a product is always created before a task can succeed) --
            # reachable only via a genuine invariant violation.
            logger.error(
                "succeeded task has no resolvable source/product",
                extra={"task_id": str(task.id)},
            )
            raise ApiError(500, "INTERNAL_ERROR", "An unexpected error occurred.")
        return {
            "task_id": str(task.id),
            "status": "completed",
            "source": source_to_dict(source),
            "product": product_to_dict(product),
            "observation": (
                current_observation_to_dict(observation) if observation is not None else None
            ),
            "created": created,
        }

    # failed or dead_letter -- doc 18 §6.6 point 6: a dead-lettered task is
    # reported using its last attempt's error_reason via the same mapping,
    # with no extra field distinguishing it from a first-attempt transient
    # failure (only ever timeout/network_error, § 5.1) -- a Phase-1 caller
    # cannot tell the two apart by response shape, only by latency, exactly
    # as doc 18 specifies.
    reason = task.error_reason or ""
    status_code, code = ERROR_REASON_TO_RESPONSE.get(reason, (500, "INTERNAL_ERROR"))
    detail = task.error_detail or {}
    message = str(detail.get("message") or f"scrape failed: {reason or 'unknown_error'}")
    details = {k: v for k, v in detail.items() if k != "message"} or None
    raise ApiError(status_code, code, message, details)


async def _poll_and_build_response(
    run_id: uuid.UUID, initial_task_id: uuid.UUID | None, response: Response
) -> dict[str, Any]:
    """Polls until the run's one task exists and reaches a terminal status,
    or the budget expires. `initial_task_id=None` covers the legacy
    alias's attach-to-in-flight case (doc 18 §6.6 point 4): an
    already-in-flight run this call just attached to may not have its task
    row yet (phase B, §4.1, racing this same request) -- folded into the
    same poll loop rather than a separate wait, so both "task already
    exists" and "task about to exist" are handled uniformly.
    """
    task_id = initial_task_id
    elapsed = 0.0
    while elapsed < MAX_WAIT_SECONDS:
        if task_id is None:
            task_id = await first_task_id_for_run(run_id)
        if task_id is not None:
            task = await _load_task(task_id)
            if task is not None and task.status in TASK_TERMINAL_STATUSES:
                return await _build_terminal_response(task)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

    logger.info("scrape did not finish within the wait budget", extra={"run_id": str(run_id)})
    response.status_code = 202
    return {"status": "pending", "task_id": str(task_id) if task_id is not None else None}


@router.post("", status_code=200)
async def create_scrape(body: ScrapeRequest, response: Response) -> dict[str, Any]:
    """doc 18 §6.6: find-or-create the source (§6.1 semantics), then
    trigger-or-attach a run for it (`attach_to_in_flight=True` -- an old,
    unmodified caller has no code path for `409 SOURCE_RUN_IN_PROGRESS`,
    so this transparently polls whatever run is already in flight instead;
    the no-overlap invariant itself, §4.4, is identical either way -- only
    the HTTP-level experience of hitting it differs).
    """
    adapter = registry.detect(body.source_url)
    if adapter is None:
        raise UnsupportedAdapterError(body.source_url)

    async with get_session() as session:
        source, _source_created = await create_or_get_source(
            session, url=body.source_url, adapter_slug=adapter.slug
        )

    run, task, _run_created = await create_manual_run(
        source_id=source.id, idempotency_key=None, attach_to_in_flight=True
    )
    return await _poll_and_build_response(
        run.id, task.id if task is not None else None, response
    )


@router.get("/{task_id}")
async def get_scrape(task_id: str, response: Response) -> dict[str, Any]:
    """doc 18 §6.6: `tasks.id` looked up directly (a plain, indexed
    `SELECT`, replacing Phase 1's Redis-result-backend-internals
    `_task_exists` hack). New-vocabulary statuses (`queued`/`in_progress`/
    `retrying`) are never surfaced here -- they all report `"pending"`,
    exactly the only two states (pending or terminal) a Phase-1-vintage
    caller has ever seen or coded against.
    """
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        raise ApiError(404, "TASK_NOT_FOUND", "No scrape task found with this id.") from None

    task = await _load_task(task_uuid)
    if task is None:
        raise ApiError(404, "TASK_NOT_FOUND", "No scrape task found with this id.")
    if task.status not in TASK_TERMINAL_STATUSES:
        return {"status": "pending", "task_id": task_id}
    return await _build_terminal_response(task)
