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
        )
    return paginated_response(
        rows, limit=effective_limit, cursor_of=lambda r: (r.created_at, r.id), serialize=run_to_dict
    )


@router.get("/{run_id}")
async def get_run_endpoint(run_id: uuid.UUID) -> dict[str, Any]:
    async with get_session() as session:
        run = await get_run(session, run_id)
    if run is None:
        raise RunNotFoundError(run_id)
    return run_to_dict(run)


@router.get("/{run_id}/tasks")
async def list_run_tasks_endpoint(run_id: uuid.UUID) -> dict[str, Any]:
    """doc 18 §6.2: cursor-paginated *in shape*, though every run has at
    most one task today (doc 18 §2.3's cardinality note). There is
    currently no second page to request, and `Task` (unlike every other
    model this package lists) has no `created_at` column to page on --
    rather than fabricate cursor semantics over a field that doesn't exist
    for a case that can't happen yet, this returns the doc 06 §1 shape
    directly (`next_cursor: null, has_more: false`) over the run's full,
    small task list. Revisit if doc 18 §9's deferred multi-task-per-run
    fan-out ever lands.
    """
    async with get_session() as session:
        run = await get_run(session, run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        tasks = await list_run_tasks(session, run_id)
    return {
        "data": [task_to_dict(t) for t in tasks],
        "pagination": {"next_cursor": None, "has_more": False},
    }
