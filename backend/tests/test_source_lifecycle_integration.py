"""doc 18 §3.1/§6.1/§8.2: source-lifecycle integration tests against a real
Postgres -- archive/unarchive as a pure status flip that never mutates
product/observation/history data, the archive-time schedule-deactivation
cascade (invariant I2), the deliberately-NOT-automatic schedule
reactivation after unarchiving, the two lifecycle-guard exceptions
(SourceArchivedError/SourceNotArchivedError), create_or_get_source()'s
idempotency -- including the archived-then-recreate case migration 0003's
own docstring flags as a self-discovered correction to doc 18's literal
schema -- and upsert_schedule()'s interval-bound enforcement (§2.2,
amendment 5).

    docker compose exec api pytest -m integration
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.api.v1.serializers import (
    current_observation_to_dict,
    observation_history_to_dict,
    product_to_dict,
)
from app.db.models.products_repository import (
    get_current_observation,
    get_product,
    list_product_history,
)
from app.db.models.repository import upsert_scrape_result
from app.db.models.scraping import Source
from app.db.models.sources_repository import (
    archive_source,
    create_or_get_source,
    get_schedule,
    get_source,
    set_source_active_or_paused,
    unarchive_source,
    upsert_schedule,
)
from app.db.session import get_session
from app.domain.errors import (
    ScheduleIntervalTooLongError,
    ScheduleIntervalTooShortError,
    SourceArchivedError,
    SourceNotArchivedError,
)
from app.scraping.types import NormalizedRecord, ValidationResult
from tests.factories import (
    create_test_run,
    create_test_source,
    create_test_task,
    unique_source_url,
)

pytestmark = pytest.mark.integration


async def _seed_scraped_product(source: Source) -> uuid.UUID:
    """Seeds one full, real scrape outcome (product + current_observation +
    the resulting first-sighting observation_history row) for `source` via
    the actual write path, upsert_scrape_result() -- not hand-inserted rows
    -- so the snapshot the test below diffs against is exactly what
    production itself would have written. `run`/`task` are created
    "completed"/"succeeded" (rather than the factories' "running"/"queued"
    defaults) purely to avoid colliding with the one-in-flight-run-per-
    source invariant (doc 18 §4.4/I5), which is unrelated to anything this
    file actually tests.
    """
    run = await create_test_run(source.id, status="completed")
    task = await create_test_task(run.id, source.id, status="succeeded")
    record = NormalizedRecord(
        product_url=source.url,
        scraped_at=datetime.now(UTC),
        stock_status="in_stock",
        product_name="Lifecycle Test Widget",
        price=Decimal("19.99"),
        currency="USD",
        sku=f"LIFECYCLE-TEST-{uuid.uuid4().hex}",
    )
    result = await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=record,
        validation=ValidationResult(is_valid=True, errors={}),
    )
    assert result.product_id is not None  # is_valid=True guarantees this
    return result.product_id


async def _snapshot(
    product_id: uuid.UUID,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Fetches and serializes (product, current_observation, history) for
    `product_id` in one consistent shape -- called both before and after
    the archive/unarchive round trip in the test below, so there is exactly
    one fetch-and-serialize code path for each side of that comparison, not
    two independently written ones that could silently drift apart.
    """
    async with get_session() as session:
        product = await get_product(session, product_id)
        observation = await get_current_observation(session, product_id)
        history = await list_product_history(session, product_id, after=None, limit=10)
    assert product is not None
    assert observation is not None
    return (
        product_to_dict(product),
        current_observation_to_dict(observation),
        [observation_history_to_dict(row) for row in history],
    )


async def test_archive_unarchive_round_trip_preserves_product_and_history_data() -> None:
    """doc 18 §8.2's "unarchive round-trip" bullet, proven directly rather
    than only argued by design: archiving and unarchiving a source is
    always a pure status flip (plus the separately-tested schedule cascade
    below) -- it must never mutate a single byte of the product/
    current_observation/observation_history rows that already existed for
    that source (sources_repository.py's own archive_source()/
    unarchive_source() docstrings make exactly this claim; this pins it
    down as an exact equality against real rows, not an assumption).
    """
    source = await create_test_source()
    product_id = await _seed_scraped_product(source)

    product_before, observation_before, history_before = await _snapshot(product_id)
    assert len(history_before) == 1  # sanity: exactly one first-sighting row

    async with get_session() as session:
        await archive_source(session, source.id)
    async with get_session() as session:
        await unarchive_source(session, source.id)

    product_after, observation_after, history_after = await _snapshot(product_id)

    assert product_after == product_before
    assert observation_after == observation_before
    assert history_after == history_before


async def test_archive_deactivates_schedule_but_keeps_the_row() -> None:
    """Invariant I2 (doc 18 §3.1): archiving cascades to
    `schedules.is_active := false` in the same transaction as the source's
    own status flip -- but the schedule row itself is never deleted, only
    flipped inactive (archive_source()'s own docstring states this cascade
    explicitly; this proves it against a real row).
    """
    source = await create_test_source()
    async with get_session() as session:
        await upsert_schedule(session, source.id, interval_minutes=15, is_active=True)

    async with get_session() as session:
        await archive_source(session, source.id)

    async with get_session() as session:
        schedule = await get_schedule(session, source.id)

    assert schedule is not None
    assert schedule.is_active is False


async def test_unarchive_leaves_schedule_inactive_until_explicit_reactivation() -> None:
    """doc 18 §3.1's "does not touch schedules" unarchive guarantee, and
    §8.2's own "Schedule reactivation is explicit" bullet: unarchiving
    alone must never flip a schedule back to `is_active=True` -- only an
    explicit `PUT .../schedule` call (upsert_schedule()) does that.

    The schedule's `next_run_at` is deliberately backdated into the past
    before archiving (the same technique
    test_run_lifecycle_integration.py's `_force_schedule_due()` helper
    uses) -- without that, the final `next_run_at > now()` assertion below
    would trivially hold even if reactivation left the old value untouched,
    since a freshly-created schedule's `next_run_at` is already comfortably
    in the future. Backdating first is what makes that assertion an actual
    proof that reactivation recomputes `next_run_at` forward, rather than a
    coincidence of test timing.
    """
    source = await create_test_source()
    async with get_session() as session:
        schedule = await upsert_schedule(session, source.id, interval_minutes=15, is_active=True)
        schedule.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    async with get_session() as session:
        await archive_source(session, source.id)
    async with get_session() as session:
        await unarchive_source(session, source.id)

    async with get_session() as session:
        schedule_after_unarchive = await get_schedule(session, source.id)
    assert schedule_after_unarchive is not None
    assert schedule_after_unarchive.is_active is False
    # unarchive doesn't touch next_run_at either -- still the backdated value.
    assert schedule_after_unarchive.next_run_at < datetime.now(UTC)

    async with get_session() as session:
        reactivated = await upsert_schedule(
            session, source.id, interval_minutes=15, is_active=True
        )

    assert reactivated.is_active is True
    assert reactivated.next_run_at > datetime.now(UTC)


async def test_active_source_only_operation_raises_while_archived() -> None:
    """doc 18 §6.1's `PATCH /sources/{id}`: `409 SOURCE_ARCHIVED` once a
    source is archived. Proven here at the repository layer only --
    set_source_active_or_paused() is the function app/api/v1/sources.py's
    PATCH route calls and translates this exception from; the HTTP-level
    mapping itself belongs to a separate test file.
    """
    source = await create_test_source()
    async with get_session() as session:
        await archive_source(session, source.id)

    async with get_session() as session:
        with pytest.raises(SourceArchivedError) as exc_info:
            await set_source_active_or_paused(session, source.id, "paused")

    assert exc_info.value.source_id == source.id


async def test_unarchive_of_a_non_archived_source_raises() -> None:
    """doc 18 §6.1's `POST /sources/{id}/unarchive`: `409
    SOURCE_NOT_ARCHIVED` when the source isn't currently archived -- proven
    directly against a plain `active` source (create_test_source()'s own
    default).
    """
    source = await create_test_source()

    async with get_session() as session:
        with pytest.raises(SourceNotArchivedError) as exc_info:
            await unarchive_source(session, source.id)

    assert exc_info.value.source_id == source.id


async def test_create_or_get_source_idempotency_including_archived_then_recreate() -> None:
    """The single most important test in this file, covering two cases:

    (a) Ordinary idempotency (doc 18 §6.1 `POST /sources`): two calls with
    the identical URL against a still-non-archived match return the SAME
    source row, `created` True then False.

    (b) Archived-then-recreate (doc 18 §6.1's "no match, or the only match
    is archived -> 201 with a new row"): migration 0003 replaced migration
    0002's plain `UNIQUE(normalized_url)` with a partial unique index,
    `uq_sources_normalized_url_active` (scoped to `status != 'archived'`)
    -- that migration's own docstring flags this as a self-discovered
    correction, since the plain constraint made this exact behavior
    impossible and the conflict went unexercised until Phase 2 introduced
    the archive concept at all. Given that history, this deserves a
    direct, end-to-end proof against a real Postgres, not just trust that
    the migration file says the right thing.
    """
    # (a) ordinary idempotency.
    url_a = unique_source_url()
    async with get_session() as session:
        source_a1, created_a1 = await create_or_get_source(
            session, url=url_a, adapter_slug="mock_store"
        )
    async with get_session() as session:
        source_a2, created_a2 = await create_or_get_source(
            session, url=url_a, adapter_slug="mock_store"
        )

    assert created_a1 is True
    assert created_a2 is False
    assert source_a2.id == source_a1.id

    # (b) archived-then-recreate: same URL, but the only existing match is
    # archived by the time the second call runs.
    url_b = unique_source_url()
    async with get_session() as session:
        source_b1, created_b1 = await create_or_get_source(
            session, url=url_b, adapter_slug="mock_store"
        )
    assert created_b1 is True

    async with get_session() as session:
        await archive_source(session, source_b1.id)

    async with get_session() as session:
        source_b2, created_b2 = await create_or_get_source(
            session, url=url_b, adapter_slug="mock_store"
        )

    assert created_b2 is True
    assert source_b2.id != source_b1.id

    # The old, archived row is additive-past, not replaced -- still exactly
    # as it was, still fetchable, still archived.
    async with get_session() as session:
        old_source = await get_source(session, source_b1.id)
    assert old_source is not None
    assert old_source.status == "archived"


@pytest.mark.parametrize(
    ("interval_minutes", "expected"),
    [
        (14, "too_short"),
        (15, "ok"),
        (10080, "ok"),
        (10081, "too_long"),
    ],
)
async def test_upsert_schedule_enforces_interval_bounds_end_to_end(
    interval_minutes: int, expected: str
) -> None:
    """doc 18 §2.2/amendment 5's 15-minute-to-7-day bound.
    test_error_reason_mapping.py::test_schedule_interval_bounds_match_doc_18_amendment_5
    already pins down the SCHEDULE_MIN_INTERVAL_MINUTES/
    SCHEDULE_MAX_INTERVAL_MINUTES constants themselves; this test instead
    proves upsert_schedule() -- the real function every write path calls --
    actually enforces them, boundary-exact (14/15/10080/10081), matching
    doc 18 §8.1's own explicit boundary-case list.
    """
    source = await create_test_source()  # fresh source per case: schedules are 1:1 with sources

    async with get_session() as session:
        if expected == "too_short":
            with pytest.raises(ScheduleIntervalTooShortError):
                await upsert_schedule(
                    session, source.id, interval_minutes=interval_minutes, is_active=True
                )
        elif expected == "too_long":
            with pytest.raises(ScheduleIntervalTooLongError):
                await upsert_schedule(
                    session, source.id, interval_minutes=interval_minutes, is_active=True
                )
        else:
            schedule = await upsert_schedule(
                session, source.id, interval_minutes=interval_minutes, is_active=True
            )
            assert schedule.interval_minutes == interval_minutes
