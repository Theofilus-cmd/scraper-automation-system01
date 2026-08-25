"""HTTP-fetch queue task stubs.

Phase 0 shipped only a trivial `ping` task (kept below, untouched) to
prove the worker process, queue routing, and broker connection all work
end-to-end. Phase 1 (doc 17) adds `scrape_source_url`, the real
fetch->parse->normalize->validate->persist pipeline, dispatched by
`POST /api/v1/scrapes` (app/api/v1/scrapes.py).

Celery invokes tasks synchronously; there is no running event loop inside
a worker process (matching app/scheduler/main.py's own sync entrypoint,
which wraps a single top-level `asyncio.run()`), so every async call this
task needs (fetch, DB) is bridged via one `asyncio.run()` per invocation.

Task results go through Celery's default JSON result serializer, which
cannot represent Decimal/datetime/UUID natively -- every such value is
converted to a plain str before being returned (`_jsonify_observation()`,
`str(...)` on ids) rather than relying on Kombu's encoder internals.
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.db.models.repository import upsert_scrape_result
from app.domain.validation import validate
from app.scraping.adapters import registry
from app.scraping.fetcher import FetchError
from app.scraping.normalize import normalize
from app.scraping.types import ExtractionSchema, FetchContext
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

# doc 05's tasks.error_reason vocabulary -> (HTTP status, doc 06 §1 error
# code) for app/api/v1/scrapes.py to use once it reads a task's result.
# missing_required_field/unsupported_adapter are the two the approved
# Phase 1 plan named explicitly; the fetch-level reasons
# (timeout/network_error/dns_or_ssrf_blocked) and parse_error complete the
# same, already-agreed vocabulary (doc 17) rather than falling back to a
# generic 500 for a perfectly well-understood outcome -- an upstream
# fetch failure is not an application bug.
ERROR_REASON_TO_RESPONSE: dict[str, tuple[int, str]] = {
    "missing_required_field": (422, "MISSING_REQUIRED_FIELD"),
    "unsupported_adapter": (422, "UNSUPPORTED_ADAPTER"),
    "parse_error": (422, "NO_PRODUCT_FOUND"),
    "timeout": (502, "FETCH_FAILED"),
    "network_error": (502, "FETCH_FAILED"),
    "dns_or_ssrf_blocked": (502, "FETCH_FAILED"),
}


@celery_app.task(name="app.workers.tasks_http.ping")
def ping() -> dict[str, str]:
    logger.info("ping task executed", extra={"queue": "http"})
    return {"queue": "http", "status": "pong"}


def _jsonify_observation(observation: dict[str, object]) -> dict[str, Any]:
    """Decimal -> str, datetime -> isoformat, everything else passes
    through unchanged. Generic over the field set on purpose, so a new
    canonical field doesn't need a matching change here.
    """
    result: dict[str, Any] = {}
    for key, value in observation.items():
        if isinstance(value, Decimal):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


async def _scrape_source_url(source_url: str) -> dict[str, Any]:
    adapter = registry.detect(source_url)
    if adapter is None:
        # Defensive: app/api/v1/scrapes.py already checks this before
        # dispatch and returns 422 without ever reaching Celery. Reachable
        # here only via a direct task call (e.g. a lower-level test).
        return {"status": "failed", "error_reason": "unsupported_adapter"}

    try:
        page = await adapter.fetch(source_url, FetchContext())
    except FetchError as exc:
        logger.warning(
            "scrape fetch failed", extra={"source_url": source_url, "reason": exc.reason}
        )
        return {"status": "failed", "error_reason": exc.reason, "message": str(exc)}

    raw_records = adapter.parse(page, ExtractionSchema.all_fields())
    if not raw_records:
        logger.warning("scrape found no product on page", extra={"source_url": source_url})
        return {"status": "failed", "error_reason": "parse_error", "message": "no product found"}

    # Phase 1 (doc 17): one URL == one product page. A future
    # listing-page adapter yielding multiple RawFields per fetch would
    # need this to fan out into multiple upserts -- not needed yet.
    record = normalize(raw_records[0], source_url)
    validation = validate(record)

    result = await upsert_scrape_result(
        source_url=source_url,
        adapter_slug=adapter.slug,
        record=record,
        validation=validation,
    )

    if not result.is_valid:
        return {
            "status": "failed",
            "error_reason": "missing_required_field",
            "validation_errors": result.validation_errors,
            "source": {"id": str(result.source_id), "url": result.source_url},
        }

    assert result.product_id is not None  # guaranteed by is_valid=True, see repository.py
    assert result.observation is not None

    return {
        "status": "completed",
        "source": {"id": str(result.source_id), "url": result.source_url},
        "product": {
            "id": str(result.product_id),
            "product_url": result.product_url,
            "product_identity_key": result.product_identity_key,
        },
        "observation": _jsonify_observation(result.observation),
        "created": result.created,
    }


@celery_app.task(name="app.workers.tasks_http.scrape_source_url")
def scrape_source_url(source_url: str) -> dict[str, Any]:
    logger.info("scrape task started", extra={"source_url": source_url, "queue": "http"})
    result: dict[str, Any] = asyncio.run(_scrape_source_url(source_url))
    logger.info(
        "scrape task finished",
        extra={"source_url": source_url, "status": result.get("status")},
    )
    return result
