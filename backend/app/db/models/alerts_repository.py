"""Workspace-scoped alert reads."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.alerts import Alert


async def list_alerts(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID | None,
    kind: str | None,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[Alert]:
    """List a workspace's alerts newest-first (alerts carry workspace_id)."""
    query = select(Alert).where(Alert.workspace_id == workspace_id)
    if source_id is not None:
        query = query.where(Alert.source_id == source_id)
    if kind is not None:
        query = query.where(Alert.kind == kind)
    if after is not None:
        after_created_at, after_id = after
        query = query.where(
            (Alert.created_at < after_created_at)
            | ((Alert.created_at == after_created_at) & (Alert.id < after_id))
        )
    query = query.order_by(Alert.created_at.desc(), Alert.id.desc()).limit(limit)
    return list((await session.execute(query)).scalars().all())
