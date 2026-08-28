"""Legacy compatibility aliases for the durable runs/tasks pipeline.

`POST /api/v1/scrapes` creates or attaches to a source run.
`GET /api/v1/scrapes/{task_id}` polls a legacy scrape task.

Both endpoints are scoped to the authenticated user's current workspace.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.dependencies import CurrentWorkspace
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
from app.db.models.runs_repository import (
    create_manual_run,
    first_task_id_for_run,
    get_task,
)
from app.db.models.sources_repository import create_or_get_source, get_source
from app.db.session import get_session
from app.domain.errors import UnsupportedAdapterError
from app.scraping.adapters import registry
from app.workers.tasks_http import ERROR_REASON_TO_RESPONSE

logger = get_logger(__name__)

router = APIRouter(prefix="/scrapes", tags=["scrapes"])

POLL_INTERVAL_SECONDS = 0.5
MAX_WAIT_SECONDS = 35.0
DEPRECATION_HEADER = "Deprecation"


class LegacyScrapesDeprecationMiddleware(BaseHTTPMiddleware):
    """Add Deprecation: true to every legacy scrape response."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
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


async def _load_task(
    task_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
) -> TaskRow | None:
    """Load a task only when its source belongs to workspace_id."""
    async with get_session() as session:
        return await get_task(
            session,
            task_id,
            workspace_id=workspace_id,
        )


async def _build_terminal_response(
    task: TaskRow,
    *,
    workspace_id: uuid.UUID,
) -> dict[str, Any]:
    """Build the legacy terminal response for an already scoped task."""
    if task.status == "succeeded":
        async with get_session() as session:
            source = await get_source(
                session,
                task.source_id,
                workspace_id=workspace_id,
            )
            product = await most_recent_product_for_source(
                session,
                task.source_id,
                workspace_id=workspace_id,
            )
            observation = (
                await get_current_observation(session, product.id)
                if product is not None
                else None
            )
            created = await task_created_new_product(session, task.id)

        if source is None or product is None:
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

    reason = task.error_reason or ""
    status_code, code = ERROR_REASON_TO_RESPONSE.get(reason, (500, "INTERNAL_ERROR"))
    detail = task.error_detail or {}
    message = str(detail.get("message") or f"scrape failed: {reason or 'unknown_error'}")
    details = {key: value for key, value in detail.items() if key != "message"} or None

    raise ApiError(status_code, code, message, details)


async def _poll_and_build_response(
    run_id: uuid.UUID,
    initial_task_id: uuid.UUID | None,
    response: Response,
    *,
    workspace_id: uuid.UUID,
) -> dict[str, Any]:
    """Poll the run's task until terminal state or the legacy wait budget."""
    task_id = initial_task_id
    elapsed = 0.0

    while elapsed < MAX_WAIT_SECONDS:
        if task_id is None:
            task_id = await first_task_id_for_run(run_id)

        if task_id is not None:
            task = await _load_task(task_id, workspace_id=workspace_id)

            if task is not None and task.status in TASK_TERMINAL_STATUSES:
                return await _build_terminal_response(
                    task,
                    workspace_id=workspace_id,
                )

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

    logger.info(
        "scrape did not finish within the wait budget",
        extra={"run_id": str(run_id)},
    )
    response.status_code = 202

    return {
        "status": "pending",
        "task_id": str(task_id) if task_id is not None else None,
    }


@router.post("", status_code=200)
async def create_scrape(
    body: ScrapeRequest,
    response: Response,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    """Create or attach to a scrape run in the authenticated workspace."""
    adapter = registry.detect(body.source_url)

    if adapter is None:
        raise UnsupportedAdapterError(body.source_url)

    async with get_session() as session:
        source, _source_created = await create_or_get_source(
            session,
            workspace_id=current_workspace.id,
            url=body.source_url,
            adapter_slug=adapter.slug,
        )

    run, task, _run_created = await create_manual_run(
        source_id=source.id,
        idempotency_key=None,
        attach_to_in_flight=True,
        workspace_id=current_workspace.id,
    )

    return await _poll_and_build_response(
        run.id,
        task.id if task is not None else None,
        response,
        workspace_id=current_workspace.id,
    )


@router.get("/{task_id}")
async def get_scrape(
    task_id: str,
    response: Response,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    """Return legacy pending/terminal scrape state for an owned task."""
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        raise ApiError(404, "TASK_NOT_FOUND", "No scrape task found with this id.") from None

    task = await _load_task(
        task_uuid,
        workspace_id=current_workspace.id,
    )

    if task is None:
        raise ApiError(404, "TASK_NOT_FOUND", "No scrape task found with this id.")

    if task.status not in TASK_TERMINAL_STATUSES:
        return {"status": "pending", "task_id": task_id}

    return await _build_terminal_response(
        task,
        workspace_id=current_workspace.id,
    )
