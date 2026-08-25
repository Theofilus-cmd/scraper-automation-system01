"""doc 06 §1's error envelope + request-ID correlation.

`{"error": {"code", "message", "details"}}` on every error response, with
a matching HTTP status. Every response -- not only error ones, a harmless
superset of doc 06 §1's "every error response also carries an
X-Request-Id header" floor -- gets an `X-Request-Id` header, so
correlating a *successful* request with its logs works too.

This is Phase 1's first real endpoint errors (health.py never raises), so
this module is small and reusable rather than one-off: an `ApiError` any
endpoint can raise, one handler each for it, for FastAPI's own
`RequestValidationError` (so a malformed request body also gets doc 06's
envelope shape instead of FastAPI's default `{"detail": [...]}`, for a
genuinely uniform contract), and a catch-all for anything unexpected.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.logging import get_logger

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"


class ApiError(Exception):
    """Raise from any endpoint to produce doc 06 §1's error envelope.

    `code` is the machine-readable string -- for scrape-related errors,
    doc 05's existing `tasks.error_reason` vocabulary, uppercased (see
    app/api/v1/scrapes.py), rather than inventing new terms Phase 3 would
    otherwise have to reconcile.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def _error_body(code: str, message: str, details: dict[str, Any] | None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"error": error}


def _request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    return existing if isinstance(existing, str) else str(uuid.uuid4())


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Stamps every response with X-Request-Id: the incoming header's
    value if the caller supplied one (preserves trace continuity across
    services), else a freshly generated uuid4. Stashed on `request.state`
    so the handlers below can echo the same id rather than minting a
    second one.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    request_id = _request_id(request)
    logger.warning(
        "api error",
        extra={"code": exc.code, "status_code": exc.status_code, "request_id": request_id},
    )
    response = JSONResponse(
        status_code=exc.status_code, content=_error_body(exc.code, exc.message, exc.details)
    )
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = _request_id(request)
    response = JSONResponse(
        status_code=422,
        content=_error_body(
            "VALIDATION_ERROR",
            "Request validation failed.",
            {"errors": jsonable_encoder(exc.errors())},
        ),
    )
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """doc 06 §1: "internal errors never leak stack traces or DB
    details." Logs the real exception server-side; the client only ever
    sees a generic, safe message.
    """
    request_id = _request_id(request)
    logger.exception("unhandled exception", extra={"request_id": request_id})
    response = JSONResponse(
        status_code=500,
        content=_error_body("INTERNAL_ERROR", "An unexpected error occurred.", None),
    )
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


def register_error_handling(app: FastAPI) -> None:
    """Call once from create_app(). Handlers are matched by exact
    exception type -- registering more than one never creates ambiguity,
    each of ApiError/RequestValidationError/Exception is independent.
    """
    app.add_middleware(RequestIdMiddleware)
    # FastAPI's add_exception_handler is typed for a generic
    # Callable[[Request, Exception], ...]; each handler below narrows the
    # second parameter to its specific exception type, which is correct
    # at runtime (FastAPI dispatches by exact registered type) but reads
    # as a variance mismatch to a strict type checker -- a known, common
    # friction point with this exact FastAPI API, not a real bug.
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore
    app.add_exception_handler(Exception, unhandled_exception_handler)
