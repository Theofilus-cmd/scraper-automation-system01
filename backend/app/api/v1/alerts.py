"""`GET /alerts` -- workspace-scoped, read-only list of detected changes."""

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
from app.api.v1.serializers import alert_to_dict
from app.core.errors import ApiError
from app.db.models.alerts import ALERT_KINDS
from app.db.models.alerts_repository import list_alerts
from app.db.session import get_session

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
async def list_alerts_endpoint(
    current_workspace: CurrentWorkspace,
    source_id: uuid.UUID | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    if kind is not None and kind not in ALERT_KINDS:
        raise ApiError(422, "INVALID_ALERT_KIND", f"kind must be one of {list(ALERT_KINDS)}.")
    after = decode_cursor_or_422(cursor)
    effective_limit = clamp_limit(limit)

    async with get_session() as session:
        rows = await list_alerts(
            session,
            workspace_id=current_workspace.id,
            source_id=source_id,
            kind=kind,
            after=after,
            limit=effective_limit + 1,
        )

    return paginated_response(
        rows,
        limit=effective_limit,
        cursor_of=lambda alert: (alert.created_at, alert.id),
        serialize=alert_to_dict,
    )
