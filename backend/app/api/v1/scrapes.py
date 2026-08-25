"""`POST /api/v1/scrapes`, `GET /api/v1/scrapes/{task_id}` -- doc 17's API
contract. An explicit precursor to doc 06 §3's full `/free/scrapes`
contract (no CSV export, no rate limiting, no 5-URL batching), deliberately
named `scrapes.py` rather than `free_scrape.py` (doc 04 §2 reserves that
name for the eventual full contract) to avoid a confusing rename later.

Imports `scraping.adapters.registry` only -- a pure, in-process function --
never `fetcher`/an adapter's `fetch()`/`parse()` directly, matching doc 04
§2's `api/`-boundary rule: the real fetch/parse/normalize/validate/persist
pipeline only ever runs inside a Celery worker
(app/workers/tasks_http.py), never in the API process.
"""

from __future__ import annotations

import asyncio
from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.core.errors import ApiError
from app.core.logging import get_logger
from app.scraping.adapters import registry
from app.workers.celery_app import celery_app
from app.workers.tasks_http import ERROR_REASON_TO_RESPONSE, scrape_source_url

logger = get_logger(__name__)

router = APIRouter(prefix="/scrapes", tags=["scrapes"])

# doc 07 §3's fetch timeout (30s total) plus a buffer for parse/normalize/
# validate/persist -- see app/scraping/fetcher.py's TOTAL_TIMEOUT_SECONDS.
POLL_INTERVAL_SECONDS = 0.5
MAX_WAIT_SECONDS = 35.0


class ScrapeRequest(BaseModel):
    source_url: str = Field(min_length=1)


async def _poll_ready(async_result: AsyncResult[Any]) -> bool:
    """`AsyncResult.ready()` performs a blocking Redis round trip under
    the hood (Celery's result backend is synchronous) -- `to_thread`
    keeps that off the event loop instead of freezing every other
    concurrent request (this endpoint's whole reason for polling instead
    of a blocking `.get(timeout=...)` in the first place).
    """
    return bool(await asyncio.to_thread(async_result.ready))


def _task_exists(task_id: str) -> bool:
    """`AsyncResult`'s public API cannot distinguish "this task_id was
    never submitted" from "queued, not started yet" -- both report state
    PENDING (a well-known Celery/Redis-backend limitation, not an
    oversight here). This reaches into the Redis result backend's
    key-value layer -- stable across recent Celery versions, though not
    part of its documented public API -- to answer the question directly,
    satisfying doc 17's "unknown/expired id -> 404" without adding a
    DB-backed job table (doc 17 Scope Decision 9).
    """
    backend = celery_app.backend
    get_key = getattr(backend, "get_key_for_task", None)
    get_value = getattr(backend, "get", None)
    if get_key is None or get_value is None:
        # Backend doesn't expose this -- degrade gracefully: an unknown
        # id then reads as "pending" rather than 404.
        return True
    return get_value(get_key(task_id)) is not None


async def _task_exists_async(task_id: str) -> bool:
    return bool(await asyncio.to_thread(_task_exists, task_id))


def _build_response(async_result: AsyncResult[Any], task_id: str) -> dict[str, Any]:
    """Only ever called once `async_result` is known `ready()` -- reading
    `.failed()`/`.result` at that point hits Celery's already-populated
    in-memory cache, not another Redis round trip, so this stays sync.
    """
    if async_result.failed():
        logger.error("scrape task raised an unhandled exception", extra={"task_id": task_id})
        raise ApiError(500, "INTERNAL_ERROR", "An unexpected error occurred.")

    payload = async_result.result
    if not isinstance(payload, dict):
        logger.error(
            "scrape task returned a non-dict result", extra={"task_id": task_id}
        )
        raise ApiError(500, "INTERNAL_ERROR", "An unexpected error occurred.")

    if payload.get("status") == "failed":
        reason = str(payload.get("error_reason", ""))
        status_code, code = ERROR_REASON_TO_RESPONSE.get(reason, (500, "INTERNAL_ERROR"))
        details = {
            k: v for k, v in payload.items() if k not in ("status", "error_reason", "message")
        }
        raise ApiError(
            status_code,
            code,
            str(payload.get("message") or f"scrape failed: {reason or 'unknown_error'}"),
            details or None,
        )

    return {"task_id": task_id, **payload}


@router.post("", status_code=200)
async def create_scrape(body: ScrapeRequest, response: Response) -> dict[str, Any]:
    adapter = registry.detect(body.source_url)
    if adapter is None:
        raise ApiError(
            422,
            "UNSUPPORTED_ADAPTER",
            "No adapter matched this URL.",
            {"source_url": body.source_url},
        )

    async_result = scrape_source_url.delay(body.source_url)
    task_id = async_result.id

    elapsed = 0.0
    while elapsed < MAX_WAIT_SECONDS:
        if await _poll_ready(async_result):
            return _build_response(async_result, task_id)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

    logger.info("scrape task did not finish within the wait budget", extra={"task_id": task_id})
    response.status_code = 202
    return {"status": "pending", "task_id": task_id}


@router.get("/{task_id}")
async def get_scrape(task_id: str) -> dict[str, Any]:
    if not await _task_exists_async(task_id):
        raise ApiError(404, "TASK_NOT_FOUND", "No scrape task found with this id.")

    async_result: AsyncResult[Any] = AsyncResult(task_id, app=celery_app)
    if not await _poll_ready(async_result):
        return {"status": "pending", "task_id": task_id}

    return _build_response(async_result, task_id)
