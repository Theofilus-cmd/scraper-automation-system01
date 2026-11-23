"""`GET /tasks` -- workspace-scoped task list (used for the dead-letter view)."""

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
from app.api.v1.serializers import task_to_dict
from app.core.errors import ApiError
from app.db.models.lifecycle import TASK_STATUSES
from app.db.models.runs_repository import list_tasks
from app.db.session import get_session

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
async def list_tasks_endpoint(
    current_workspace: CurrentWorkspace,
    status: str | None = None,
    source_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    if status is not None and status not in TASK_STATUSES:
        raise ApiError(422, "INVALID_TASK_STATUS", f"status must be one of {list(TASK_STATUSES)}.")
    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)

    async with get_session() as session:
        rows = await list_tasks(
            session,
            workspace_id=current_workspace.id,
            status=status,
            source_id=source_id,
            after=after,
            limit=effective_limit + 1,
        )

    # Rows are (Task, run_created_at): tasks have no created_at of their own,
    # and each run owns exactly one task, so the run's created_at is a stable
    # newest-first key (Task.id breaks ties).
    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda row: (row[1], row[0].id),
        serialize=lambda row: task_to_dict(row[0]),
    )
