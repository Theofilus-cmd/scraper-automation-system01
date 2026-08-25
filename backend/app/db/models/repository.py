"""upsert_scrape_result() -- doc 08 §4's write-gating rule (doc 17's Data
model section) plus doc 18 §3.3's observation-history diff-and-append. The
only place `products` / `current_observations` / `observation_history` are
ever written.

Phase 2 change from Phase 1, flagged plainly rather than silently rewritten:
this function used to also unconditionally upsert a `sources` row on every
call (bookkeeping -- "what URL did we try"). That's obsolete now that every
scrape's `source_id` is already resolved once, up front, at run-creation
time (`app/db/models/sources_repository.py::create_or_get_source()`,
called by the API layer before a run/task ever exists) -- by the time a
task executes, its source is guaranteed to already exist. This function now
takes an already-resolved `source_id` directly instead of a raw URL, and
never touches `sources` at all. `app/workers/tasks_http.py` is the only
caller.

The write-gating rule (doc 08 §4, unchanged in substance) only ever governs
`products`/`current_observations`/`observation_history` -- when validation
fails, this function writes nothing at all (not even the old `sources`
upsert, since there's no source-upsert step left to run).

Idempotent throughout via `INSERT ... ON CONFLICT DO UPDATE`, unchanged
from Phase 1.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.lifecycle import TRACKED_OBSERVATION_FIELDS, ObservationHistory
from app.db.models.scraping import CurrentObservation, Product
from app.db.session import get_session
from app.scraping.types import NormalizedRecord, ValidationResult

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScrapeUpsertResult:
    """Everything the worker needs to finalize the owning task/run's status
    (`app/workers/tasks_http.py`) after this write commits.

    Deliberately does NOT carry the HTTP-facing response body: doc 18
    §6.6's legacy alias and the new `GET /runs/{id}` family both re-query
    `products`/`current_observations` fresh from the database whenever they
    need one, rather than threading a snapshot through from the write --
    the database, not this in-process return value, is the single source
    of truth once this function's transaction has committed. This object
    stays small and purely about "did the write succeed, and how".
    """

    is_valid: bool
    source_id: uuid.UUID
    validation_errors: dict[str, str] = field(default_factory=dict)
    product_id: uuid.UUID | None = None
    created: bool = False
    history_appended: bool = False


def _compute_product_identity_key(record: NormalizedRecord) -> str:
    """doc 05 §6 / doc 10 §1 precedence: `sku`, else `product_id`, else
    normalized `product_url` (already normalized -- `normalize()` sets
    `NormalizedRecord.product_url` via `normalize_url()`).

    Prefixed by kind (`sku:`/`product_id:`/`url:`) to rule out a same-string
    collision between, say, one product's `sku` and an unrelated product's
    `product_id` -- unchanged from Phase 1.
    """
    if record.sku:
        return f"sku:{record.sku}"
    if record.product_id:
        return f"product_id:{record.product_id}"
    return f"url:{record.product_url}"


async def _upsert_product(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    identity_key: str,
    product_url: str,
    sku: str | None,
    source_product_id: str | None,
) -> tuple[uuid.UUID, bool]:
    """Returns `(product_id, created)`, unchanged from Phase 1 (Postgres's
    `xmax = 0` idiom, one round trip, no race window). Its
    `ON CONFLICT DO UPDATE` also does load-bearing work for Phase 2: the
    implicit row lock it takes on an already-existing product is what
    safely serializes two genuinely concurrent writers to the same product
    identity before either reaches the observation-history diff below --
    see `_diff_against_prior_observation`'s docstring.
    """
    table = Product.__table__
    values = {
        "source_id": source_id,
        "product_identity_key": identity_key,
        "product_url": product_url,
        "sku": sku,
        "source_product_id": source_product_id,
    }
    stmt = pg_insert(table).values(**values)
    update_columns = {
        k: getattr(stmt.excluded, k) for k in ("product_url", "sku", "source_product_id")
    }
    stmt = stmt.on_conflict_do_update(
        constraint="uq_products_source_identity", set_=update_columns
    ).returning(table.c.id, text("(xmax = 0) AS created"))

    row = (await session.execute(stmt)).one()
    product_id: uuid.UUID = row.id
    created: bool = bool(row.created)
    return product_id, created


def _json_safe(value: object) -> object:
    """`change_summary` is JSONB -- Decimal/datetime/date aren't natively
    JSON-serializable, so both sides of every diff entry go through this
    (mirrors `app/workers/tasks_http.py::_jsonify_observation()`'s
    conversions; applied here instead since this runs inside the same
    transaction as the write, before anything crosses the worker/API
    process boundary).
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _new_observation_values(record: NormalizedRecord) -> dict[str, object]:
    return {name: getattr(record, name) for name in TRACKED_OBSERVATION_FIELDS}


async def _diff_against_prior_observation(
    session: AsyncSession, *, product_id: uuid.UUID, new_values: dict[str, object]
) -> tuple[dict[str, object] | None, bool]:
    """doc 18 §3.3: locks the existing `current_observations` row (if any)
    `FOR UPDATE` and computes the diff against `new_values`. Returns
    `(change_summary_or_none, should_append)` -- `should_append` is True
    iff (a) no prior row existed (first sighting: `change_summary` is
    `None`) or (b) at least one tracked field differs (`change_summary` is
    the non-empty diff dict); False when a prior row existed and nothing
    tracked changed.

    MUST be called strictly after `_upsert_product` for the same product
    (race-safety note, since the ordering here is load-bearing and not
    obvious in isolation): by that point, `_upsert_product` has already
    either inserted a brand-new `products` row (in which case no
    `current_observations` row can possibly exist yet -- product and
    observation are always created together, never independently) or taken
    an implicit row lock on an existing product via its own
    `ON CONFLICT DO UPDATE` (which blocks a second, genuinely concurrent
    writer to the SAME identity until the first writer's whole transaction
    commits). Either way, by the time this function's `SELECT ... FOR
    UPDATE` runs, any other concurrent writer to the same product is
    already serialized behind it -- so two truly simultaneous
    first-sightings for the same brand-new identity cannot both compute
    `prior=None` and each append a duplicate "first sighting" history row.
    """
    prior = (
        await session.execute(
            select(CurrentObservation)
            .where(CurrentObservation.product_id == product_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if prior is None:
        return None, True

    diffs: dict[str, object] = {
        name: {"from": _json_safe(getattr(prior, name)), "to": _json_safe(new_values[name])}
        for name in TRACKED_OBSERVATION_FIELDS
        if getattr(prior, name) != new_values[name]
    }
    change_summary: dict[str, object] | None = diffs or None
    return change_summary, bool(diffs)


async def _upsert_current_observation(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    new_values: dict[str, object],
    validation: ValidationResult,
    scraped_at: datetime,
) -> None:
    table = CurrentObservation.__table__
    values: dict[str, object] = {
        **new_values,
        "product_id": product_id,
        "is_valid": validation.is_valid,
        "validation_errors": validation.errors or None,
        "scraped_at": scraped_at,
    }
    stmt = pg_insert(table).values(**values)
    update_columns = {k: getattr(stmt.excluded, k) for k in values if k != "product_id"}
    stmt = stmt.on_conflict_do_update(index_elements=["product_id"], set_=update_columns)
    await session.execute(stmt)


async def upsert_scrape_result(
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID,
    record: NormalizedRecord,
    validation: ValidationResult,
) -> ScrapeUpsertResult:
    """The one entry point for persisting a scrape outcome. See module
    docstring for the write-gating rule and the Phase 2 signature change.
    `run_id`/`task_id` are provenance for the `observation_history` row
    doc 18 §2.4 requires them on -- never used to gate anything here.
    """
    async with get_session() as session:
        if not validation.is_valid:
            logger.info(
                "scrape validation failed -- no product/observation/history row written",
                extra={"source_id": str(source_id), "errors": validation.errors},
            )
            return ScrapeUpsertResult(
                is_valid=False, source_id=source_id, validation_errors=validation.errors
            )

        # Guaranteed non-None by validate() (app/domain/validation.py) --
        # is_valid=True above means every required field, including these
        # three, passed its required-field check. Narrowed explicitly
        # (rather than trusted implicitly) so the ObservationHistory
        # construction below can use `record`'s fields directly with their
        # real, precise types instead of routing through a loosely-typed
        # intermediate dict.
        assert record.product_name is not None
        assert record.price is not None
        assert record.currency is not None

        identity_key = _compute_product_identity_key(record)
        product_id, created = await _upsert_product(
            session,
            source_id=source_id,
            identity_key=identity_key,
            product_url=record.product_url,
            sku=record.sku,
            source_product_id=record.product_id,
        )

        new_values = _new_observation_values(record)
        change_summary, should_append = await _diff_against_prior_observation(
            session, product_id=product_id, new_values=new_values
        )
        await _upsert_current_observation(
            session,
            product_id=product_id,
            new_values=new_values,
            validation=validation,
            scraped_at=record.scraped_at,
        )

        if should_append:
            session.add(
                ObservationHistory(
                    product_id=product_id,
                    run_id=run_id,
                    task_id=task_id,
                    change_summary=change_summary,
                    product_name=record.product_name,
                    brand=record.brand,
                    category=record.category,
                    price=record.price,
                    currency=record.currency,
                    original_price=record.original_price,
                    discount=record.discount,
                    variant=record.variant,
                    stock_status=record.stock_status,
                    rating=record.rating,
                    review_count=record.review_count,
                    description=record.description,
                    image_url=record.image_url,
                    is_valid=validation.is_valid,
                    validation_errors=validation.errors or None,
                    scraped_at=record.scraped_at,
                )
            )

        await session.commit()
        logger.info(
            "scrape result persisted",
            extra={
                "source_id": str(source_id),
                "product_id": str(product_id),
                "product_created": created,
                "history_appended": should_append,
            },
        )

        return ScrapeUpsertResult(
            is_valid=True,
            source_id=source_id,
            validation_errors=validation.errors,
            product_id=product_id,
            created=created,
            history_appended=should_append,
        )
