"""Source lifecycle + schedule repository functions (doc 18 §3.1, §6.1).

Every function here opens and commits its own transaction (matching Phase
1's `repository.py::upsert_scrape_result()` convention) unless a `session`
is passed in explicitly by a caller that needs to compose several of these
into one larger transaction (the run-creation path in runs_repository.py
does this for the create-or-get-source + create-run sequence).

Domain exceptions (app/domain/errors.py) are raised for every documented
failure mode in doc 18 §6.1/§6.4; `app/api/v1/sources.py` is the only
place that catches them and produces the matching `ApiError`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
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


async def create_or_get_source(
    session: AsyncSession, *, url: str, adapter_slug: str
) -> tuple[Source, bool]:
    """doc 18 §6.1 `POST /sources` / §6.6's legacy find-or-create: idempotent
    among non-archived rows sharing this `normalized_url`. No match, or the
    only match is archived -> a new row is created (`201`); a non-archived
    match is returned as-is (`200`). Returns `(source, created)`.

    Runs the create-time SSRF check (doc 18 §7.1) before touching the
    database at all -- a request for a URL that will never be fetchable
    shouldn't leave a `sources` row behind.

    Two-layer race handling, same shape as the no-overlap-runs invariant
    (doc 18 §4.4): the SELECT below is the common-case fast path; migration
    0003's `uq_sources_normalized_url_active` partial unique index is the
    actual, race-free backstop for two concurrent callers creating the
    "same" new source at once.
    """
    try:
        validate_source_url(url)
    except FetchError as exc:
        raise SsrfBlockedError(url, str(exc)) from exc

    normalized = normalize_url(url)

    existing: Source | None = (
        await session.execute(
            select(Source).where(Source.normalized_url == normalized, Source.status != "archived")
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    table = Source.__table__
    stmt = (
        pg_insert(table)
        .values(url=url, normalized_url=normalized, adapter_type=adapter_slug, status="active")
        .returning(table.c.id)
    )
    try:
        source_id: uuid.UUID = (await session.execute(stmt)).scalar_one()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        winner: Source = (
            await session.execute(
                select(Source).where(
                    Source.normalized_url == normalized, Source.status != "archived"
                )
            )
        ).scalar_one()
        return winner, False

    created_source: Source = (
        await session.execute(select(Source).where(Source.id == source_id))
    ).scalar_one()
    return created_source, True


async def get_source(session: AsyncSession, source_id: uuid.UUID) -> Source | None:
    result: Source | None = (
        await session.execute(select(Source).where(Source.id == source_id))
    ).scalar_one_or_none()
    return result


async def list_sources(
    session: AsyncSession,
    *,
    status: str | None,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[Source]:
    query = select(Source).order_by(Source.created_at.desc(), Source.id.desc()).limit(limit)
    if status is not None:
        query = query.where(Source.status == status)
    if after is not None:
        after_created_at, after_id = after
        query = query.where(
            (Source.created_at < after_created_at)
            | ((Source.created_at == after_created_at) & (Source.id < after_id))
        )
    return list((await session.execute(query)).scalars().all())


async def _require_source(session: AsyncSession, source_id: uuid.UUID) -> Source:
    source = await get_source(session, source_id)
    if source is None:
        raise SourceNotFoundError(source_id)
    return source


async def set_source_active_or_paused(
    session: AsyncSession, source_id: uuid.UUID, new_status: str
) -> Source:
    """doc 18 §6.1 `PATCH /sources/{id}` -- `new_status` is `"active"` or
    `"paused"` only (validated by the API layer's request schema; archived
    is reached only via DELETE/unarchive, never this path)."""
    source = await _require_source(session, source_id)
    if source.status == "archived":
        raise SourceArchivedError(source_id)
    source.status = new_status
    await session.commit()
    return source


async def archive_source(session: AsyncSession, source_id: uuid.UUID) -> Source:
    """doc 18 §3.1/§6.1 `DELETE /sources/{id}`: archives, idempotent.
    Cascades to `schedules.is_active := false` in the same transaction
    (invariant I2) -- never touches `products`/`current_observations`/
    `runs`/`tasks`/`observation_history`.
    """
    source = await _require_source(session, source_id)
    if source.status == "archived":
        return source  # idempotent -- 204 either way, no-op on a repeat call
    source.status = "archived"
    schedule = (
        await session.execute(select(Schedule).where(Schedule.source_id == source_id))
    ).scalar_one_or_none()
    if schedule is not None:
        schedule.is_active = False
    await session.commit()
    return source


async def unarchive_source(session: AsyncSession, source_id: uuid.UUID) -> Source:
    """doc 18 §3.1/§6.1 `POST /sources/{id}/unarchive`: flips
    `sources.status: archived -> active` and NOTHING else. Does not touch
    `schedules` (an inactive schedule stays inactive -- explicit
    reactivation is a separate `PUT .../schedule` call, § below) or any
    product/observation/run/task/history data, because archiving never
    touched those either (see this function's docstring counterpart,
    `archive_source`, above).
    """
    source = await _require_source(session, source_id)
    if source.status != "archived":
        raise SourceNotArchivedError(source_id)
    source.status = "active"
    await session.commit()
    return source


async def get_schedule(session: AsyncSession, source_id: uuid.UUID) -> Schedule | None:
    result: Schedule | None = (
        await session.execute(select(Schedule).where(Schedule.source_id == source_id))
    ).scalar_one_or_none()
    return result


async def upsert_schedule(
    session: AsyncSession, source_id: uuid.UUID, *, interval_minutes: int, is_active: bool
) -> Schedule:
    """doc 18 §2.2/§6.1 `PUT /sources/{id}/schedule`. App-level bounds check
    (422) paired with migration 0003's `ck_schedules_interval_minutes` CHECK
    as the DB-level backstop -- the same layered-validation shape used
    throughout this design (SSRF, no-overlap-runs).

    `next_run_at` is (re)computed to `now() + interval_minutes` whenever
    this call transitions the schedule into `is_active=True` (a fresh
    create, or the explicit reactivation path doc 18 §3.1 describes after
    an unarchive) -- deliberately, so a schedule that sat inactive for a
    long time doesn't come back reporting itself "due since forever" the
    instant it's reactivated. Left untouched when the schedule is already
    active and only `interval_minutes` is changing: the new interval takes
    effect starting from the *next* firing (the scheduler's claim query,
    §4.1, always recomputes `next_run_at` from whatever `interval_minutes`
    currently holds at claim time) -- doc 18 doesn't specify this exact
    initial-`next_run_at` policy explicitly; this is a deliberate, flagged
    judgment call, not a literal transcription of the design doc.
    """
    if interval_minutes < SCHEDULE_MIN_INTERVAL_MINUTES:
        raise ScheduleIntervalTooShortError(interval_minutes, SCHEDULE_MIN_INTERVAL_MINUTES)
    if interval_minutes > SCHEDULE_MAX_INTERVAL_MINUTES:
        raise ScheduleIntervalTooLongError(interval_minutes, SCHEDULE_MAX_INTERVAL_MINUTES)

    source = await _require_source(session, source_id)
    if source.status == "archived":
        raise SourceArchivedError(source_id)

    schedule = await get_schedule(session, source_id)
    now = _utcnow()
    if schedule is None:
        schedule = Schedule(
            source_id=source_id,
            interval_minutes=interval_minutes,
            is_active=is_active,
            next_run_at=now + timedelta(minutes=interval_minutes) if is_active else now,
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


async def delete_schedule(session: AsyncSession, source_id: uuid.UUID) -> None:
    """doc 18 §6.1 `DELETE /sources/{id}/schedule` -- idempotent, 204
    either way."""
    schedule = await get_schedule(session, source_id)
    if schedule is not None:
        await session.delete(schedule)
        await session.commit()


async def require_schedule(session: AsyncSession, source_id: uuid.UUID) -> Schedule:
    schedule = await get_schedule(session, source_id)
    if schedule is None:
        raise ScheduleNotFoundError(source_id)
    return schedule


async def create_or_get_source_new_session(*, url: str, adapter_slug: str) -> tuple[Source, bool]:
    """Convenience wrapper for callers (the legacy alias, §6.6) that don't
    already hold an open session -- opens one, delegates, closes it."""
    async with get_session() as session:
        return await create_or_get_source(session, url=url, adapter_slug=adapter_slug)
