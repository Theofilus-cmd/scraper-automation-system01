"""Translates app/domain/errors.py exceptions into the doc 06 §1 error
envelope (ApiError) -- the one place this mapping lives, per every router
in this package (doc 18 §6.4's error-code table).
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.errors import ApiError, api_error_handler
from app.domain.errors import (
    DomainError,
    ProductNotFoundError,
    RunInProgressError,
    RunNotFoundError,
    ScheduleIntervalTooLongError,
    ScheduleIntervalTooShortError,
    ScheduleNotFoundError,
    SourceArchivedError,
    SourceNotArchivedError,
    SourceNotFoundError,
    SsrfBlockedError,
    UnsupportedAdapterError,
)


def raise_for_domain_error(exc: DomainError) -> NoReturn:
    if isinstance(exc, SourceNotFoundError):
        raise ApiError(404, "SOURCE_NOT_FOUND", "No source found with this id.") from exc
    if isinstance(exc, SourceArchivedError):
        raise ApiError(409, "SOURCE_ARCHIVED", "This source is archived.") from exc
    if isinstance(exc, SourceNotArchivedError):
        raise ApiError(409, "SOURCE_NOT_ARCHIVED", "This source is not archived.") from exc
    if isinstance(exc, ScheduleNotFoundError):
        raise ApiError(404, "SCHEDULE_NOT_FOUND", "No schedule exists for this source.") from exc
    if isinstance(exc, ScheduleIntervalTooShortError):
        raise ApiError(
            422,
            "SCHEDULE_INTERVAL_TOO_SHORT",
            f"interval_minutes must be at least {exc.minimum}.",
            {"interval_minutes": exc.interval_minutes, "minimum": exc.minimum},
        ) from exc
    if isinstance(exc, ScheduleIntervalTooLongError):
        raise ApiError(
            422,
            "SCHEDULE_INTERVAL_TOO_LONG",
            f"interval_minutes must be at most {exc.maximum}.",
            {"interval_minutes": exc.interval_minutes, "maximum": exc.maximum},
        ) from exc
    if isinstance(exc, RunNotFoundError):
        raise ApiError(404, "RUN_NOT_FOUND", "No run found with this id.") from exc
    if isinstance(exc, RunInProgressError):
        raise ApiError(
            409,
            "SOURCE_RUN_IN_PROGRESS",
            "This source already has a run in progress.",
            {"run_id": str(exc.existing_run_id)},
        ) from exc
    if isinstance(exc, ProductNotFoundError):
        raise ApiError(404, "PRODUCT_NOT_FOUND", "No product found with this id.") from exc
    if isinstance(exc, UnsupportedAdapterError):
        raise ApiError(
            422, "UNSUPPORTED_ADAPTER", "No adapter matched this URL.", {"source_url": exc.url}
        ) from exc
    if isinstance(exc, SsrfBlockedError):
        raise ApiError(422, "DNS_OR_SSRF_BLOCKED", str(exc), {"source_url": exc.url}) from exc
    raise ApiError(500, "INTERNAL_ERROR", "An unexpected error occurred.") from exc


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """Registered in main.py alongside `register_error_handling()` (doc 06
    §1's existing ApiError/RequestValidationError/Exception handlers) --
    lets every router raise a domain exception directly and never repeat
    `try/except DomainError: raise_for_domain_error(...)` at each call
    site. `raise_for_domain_error` stays the one place the mapping itself
    is defined; this only adapts it to FastAPI's handler shape, reusing
    `api_error_handler` rather than duplicating its response-building.
    """
    try:
        raise_for_domain_error(exc)
    except ApiError as api_exc:
        return await api_error_handler(request, api_exc)
