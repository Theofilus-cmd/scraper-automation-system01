"""`GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/tasks` -- doc 18 §6.2.
`POST /sources/{id}/runs` (the trigger endpoint) lives in sources.py
instead, colocated with the rest of "act on this source" -- this file/
sources.py split is this implementation's own organization, not something
doc 18 itself prescribes.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from app.api.dependencies import CurrentWorkspace
from app.api.v1.pagination import (
    DEFAULT_LIMIT,
    clamp_limit,
    decode_cursor_or_422,
    paginated_response,
)
from app.api.v1.serializers import run_to_dict, task_to_dict
from app.db.models.runs_repository import get_run, list_run_tasks, list_runs
from app.db.session import get_session
from app.domain.errors import RunNotFoundError

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("")
async def list_runs_endpoint(
    current_workspace: CurrentWorkspace,
    source_id: uuid.UUID | None = None,
    status: str | None = None,
    triggered_by: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)

    async with get_session() as session:
        rows = await list_runs(
            session,
            source_id=source_id,
            status=status,
            triggered_by=triggered_by,
            after=after,
            limit=effective_limit + 1,
            workspace_id=current_workspace.id,
        )

    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda run: (run.created_at, run.id),
        serialize=run_to_dict,
    )


@router.get("/{run_id}")
async def get_run_endpoint(
    run_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    async with get_session() as session:
        run = await get_run(
            session,
            run_id,
            workspace_id=current_workspace.id,
        )

    if run is None:
        raise RunNotFoundError(run_id)

    return run_to_dict(run)


@router.get("/{run_id}/tasks")
async def list_run_tasks_endpoint(
    run_id: uuid.UUID,
    current_workspace: CurrentWorkspace,
) -> dict[str, Any]:
    """Return the run's task list in the documented pagination shape."""
    async with get_session() as session:
        run = await get_run(
            session,
            run_id,
            workspace_id=current_workspace.id,
        )

        if run is None:
            raise RunNotFoundError(run_id)

        tasks = await list_run_tasks(
            session,
            run_id,
            workspace_id=current_workspace.id,
        )

    return {
        "data": [task_to_dict(task) for task in tasks],
        "pagination": {"next_cursor": None, "has_more": False},
    }
