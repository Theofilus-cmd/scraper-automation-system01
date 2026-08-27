"""Source lifecycle and schedule repository functions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.identity import Workspace
from app.db.models.lifecycle import (
    SCHEDULE_MAX_INTERVAL_MINUTES,
    SCHEDULE_MIN_INTERVAL_MINUTES,
    Schedule,
)
from app.db.models.scraping import Source
from app.db.session import get_session
from app.domain.errors import (
    ScheduleIntervalTooLongError,
    ScheduleIntervalTooShortError,
    ScheduleNotFoundError,
    SourceArchivedError,
    SourceNotArchivedError,
    SourceNotFoundError,
    SsrfBlockedError,
)
from app.scraping.fetcher import FetchError, validate_source_url
from app.scraping.normalize import normalize_url

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _resolve_workspace_id(
    session: AsyncSession,
    workspace_id: uuid.UUID | None,
) -> uuid.UUID:
    """Return the explicit workspace, or the legacy test workspace.

    New application paths must always pass ``workspace_id``. The fallback is
    solely for pre-workspace repository callers/tests that still use the
    historical function signature. It never creates a workspace.
    """
    if workspace_id is not None:
        return workspace_id

    legacy_workspace_id = (
        await session.execute(
            select(Workspace.id).order_by(Workspace.created_at.asc()).limit(1)
        )
    ).scalar_one_or_none()

    if legacy_workspace_id is None:
        raise RuntimeError(
            "workspace_id is required when no workspace exists for legacy callers"
        )

    return legacy_workspace_id


async def create_or_get_source(
    session: AsyncSession,
    *,
    url: str,
    adapter_slug: str,
    workspace_id: uuid.UUID | None = None,
) -> tuple[Source, bool]:
    """Create or return a matching non-archived source within a workspace."""
    try:
        validate_source_url(url)
    except FetchError as exc:
        raise SsrfBlockedError(url, str(exc)) from exc

    resolved_workspace_id = await _resolve_workspace_id(session, workspace_id)
    normalized = normalize_url(url)

    existing = (
        await session.execute(
            select(Source).where(
                Source.workspace_id == resolved_workspace_id,
                Source.normalized_url == normalized,
                Source.status != "archived",
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        return existing, False

    source = Source(
        workspace_id=resolved_workspace_id,
        url=url,
        normalized_url=normalized,
        adapter_type=adapter_slug,
        status="active",
    )
    session.add(source)

    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()

        winner = (
            await session.execute(
                select(Source).where(
                    Source.workspace_id == resolved_workspace_id,
                    Source.normalized_url == normalized,
                    Source.status != "archived",
                )
            )
        ).scalar_one_or_none()

        if winner is None:
            raise

        return winner, False

    await session.refresh(source)
    return source, True


async def get_source(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Source | None:
    """Look up a source, optionally restricting it to one workspace."""
    query = select(Source).where(Source.id == source_id)
    if workspace_id is not None:
        query = query.where(Source.workspace_id == workspace_id)
    return (await session.execute(query)).scalar_one_or_none()


async def list_sources(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    status: str | None,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[Source]:
    """List sources owned by one workspace, newest first."""
    query = (
        select(Source)
        .where(Source.workspace_id == workspace_id)
        .order_by(Source.created_at.desc(), Source.id.desc())
        .limit(limit)
    )

    if status is not None:
        query = query.where(Source.status == status)

    if after is not None:
        after_created_at, after_id = after
        query = query.where(
            (Source.created_at < after_created_at)
            | ((Source.created_at == after_created_at) & (Source.id < after_id))
        )

    return list((await session.execute(query)).scalars().all())


async def _require_source(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Source:
    source = await get_source(session, source_id, workspace_id=workspace_id)
    if source is None:
        raise SourceNotFoundError(source_id)
    return source


async def set_source_active_or_paused(
    session: AsyncSession,
    source_id: uuid.UUID,
    new_status: str,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Source:
    """Set a source to active or paused."""
    source = await _require_source(session, source_id, workspace_id=workspace_id)

    if source.status == "archived":
        raise SourceArchivedError(source_id)

    source.status = new_status
    await session.commit()
    return source


async def archive_source(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Source:
    """Archive a source and disable its schedule."""
    source = await _require_source(session, source_id, workspace_id=workspace_id)

    if source.status == "archived":
        return source

    source.status = "archived"

    schedule = (
        await session.execute(select(Schedule).where(Schedule.source_id == source_id))
    ).scalar_one_or_none()

    if schedule is not None:
        schedule.is_active = False

    await session.commit()
    return source


async def unarchive_source(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Source:
    """Unarchive a source."""
    source = await _require_source(session, source_id, workspace_id=workspace_id)

    if source.status != "archived":
        raise SourceNotArchivedError(source_id)

    source.status = "active"
    await session.commit()
    return source


async def get_schedule(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Schedule | None:
    """Return a source schedule, optionally requiring workspace ownership."""
    if workspace_id is not None:
        source = await get_source(session, source_id, workspace_id=workspace_id)
        if source is None:
            return None

    return (
        await session.execute(select(Schedule).where(Schedule.source_id == source_id))
    ).scalar_one_or_none()


async def upsert_schedule(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    interval_minutes: int,
    is_active: bool,
    workspace_id: uuid.UUID | None = None,
) -> Schedule:
    """Create or update a source schedule."""
    if interval_minutes < SCHEDULE_MIN_INTERVAL_MINUTES:
        raise ScheduleIntervalTooShortError(
            interval_minutes,
            SCHEDULE_MIN_INTERVAL_MINUTES,
        )

    if interval_minutes > SCHEDULE_MAX_INTERVAL_MINUTES:
        raise ScheduleIntervalTooLongError(
            interval_minutes,
            SCHEDULE_MAX_INTERVAL_MINUTES,
        )

    source = await _require_source(session, source_id, workspace_id=workspace_id)

    if source.status == "archived":
        raise SourceArchivedError(source_id)

    schedule = await get_schedule(session, source_id)
    now = _utcnow()

    if schedule is None:
        schedule = Schedule(
            source_id=source_id,
            interval_minutes=interval_minutes,
            is_active=is_active,
            next_run_at=(
                now + timedelta(minutes=interval_minutes) if is_active else now
            ),
        )
        session.add(schedule)
    else:
        reactivating = is_active and not schedule.is_active
        schedule.interval_minutes = interval_minutes
        schedule.is_active = is_active

        if reactivating:
            schedule.next_run_at = now + timedelta(minutes=interval_minutes)

    await session.commit()
    return schedule


async def delete_schedule(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> None:
    """Delete a schedule; optionally enforce workspace ownership."""
    if workspace_id is not None:
        source = await get_source(session, source_id, workspace_id=workspace_id)
        if source is None:
            raise SourceNotFoundError(source_id)

    schedule = await get_schedule(session, source_id)

    if schedule is not None:
        await session.delete(schedule)
        await session.commit()


async def require_schedule(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Schedule:
    """Return a schedule or raise the documented not-found domain error."""
    schedule = await get_schedule(
        session,
        source_id,
        workspace_id=workspace_id,
    )

    if schedule is None:
        raise ScheduleNotFoundError(source_id)

    return schedule


async def create_or_get_source_new_session(
    *,
    url: str,
    adapter_slug: str,
    workspace_id: uuid.UUID | None = None,
) -> tuple[Source, bool]:
    """Convenience wrapper for callers without an existing session."""
    async with get_session() as session:
        return await create_or_get_source(
            session,
            url=url,
            adapter_slug=adapter_slug,
            workspace_id=workspace_id,
        )