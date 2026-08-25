"""ORM models for Phase 2's scheduling/run/history tables (migration
`0003`): `Schedule`, `Run`, `Task`, `ObservationHistory` -- doc 18 §2.2-§2.4.

Column shapes mirror migration `0003` exactly, same hand-in-hand convention
Phase 1 established between `app/db/models/scraping.py` and migration
`0002` (kept in sync by hand -- no autogenerate tooling exists yet).

Deliberately NO `relationship()` declarations between these models (unlike
`Source.products`/`Product.current_observation` in scraping.py). Two
reasons: (1) every real read path in this codebase already queries
directly rather than traversing relationships (see repository.py,
runs_repository.py, sources_repository.py) -- there is no code that would
ever exercise a lazy-load; adding relationships that are never used is
just surface area to keep in sync for no benefit. (2) triggering a lazy
load on an `AsyncSession`-backed relationship outside an active `await`
context raises `MissingGreenlet` -- a real footgun this codebase has
avoided entirely so far by never doing it, and there's no reason to
introduce the first instance now. Every "nested" API response shape (doc
18 §6.1's `GET /sources/{id}` embedding `schedule`/`current_product`) is
built by the API layer issuing explicit, separate queries instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CHAR, CheckConstraint, ForeignKey, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

SCHEDULE_MIN_INTERVAL_MINUTES = 15
SCHEDULE_MAX_INTERVAL_MINUTES = 10080  # 7 days -- doc 18 §2.2, amendment 5

RUN_STATUSES = ("pending", "running", "completed", "completed_with_errors", "failed", "cancelled")
RUN_IN_FLIGHT_STATUSES = ("pending", "running")
RUN_TRIGGERED_BY = ("schedule", "manual")

TASK_STATUSES = ("queued", "in_progress", "succeeded", "failed", "retrying", "dead_letter")
# Added alongside the API layer (commit 4): the three TASK_STATUSES values
# a task never leaves once reached -- app/api/v1/scrapes.py's legacy poll
# loop (doc 18 §6.6) and any future caller need this same terminal/pending
# split, so it's defined once here rather than re-hardcoded per caller.
TASK_TERMINAL_STATUSES = ("succeeded", "failed", "dead_letter")
TASK_TRANSIENT_REASONS = ("timeout", "network_error")  # doc 18 §5.1 -- retryable
TASK_PERMANENT_REASONS = (
    "missing_required_field",
    "unsupported_adapter",
    "parse_error",
    "dns_or_ssrf_blocked",
    "source_archived",
)  # doc 18 §5.1/§3.2 -- deterministic, never retried


class Schedule(Base):
    """1:1 with `sources` (doc 18 §2.2) -- one schedule per source, unlike
    doc 05 §4's many-schedules-per-target shape. `source_id UNIQUE` is the
    literal enforcement of that 1:1.
    """

    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint(
            f"interval_minutes BETWEEN {SCHEDULE_MIN_INTERVAL_MINUTES} "
            f"AND {SCHEDULE_MAX_INTERVAL_MINUTES}",
            name="ck_schedules_interval_minutes",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    interval_minutes: Mapped[int] = mapped_column(nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    next_run_at: Mapped[datetime] = mapped_column(nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class Run(Base):
    """One firing of a schedule, or one manual/legacy trigger (doc 18 §2.3,
    §3.2). `uq_runs_one_in_flight_per_source` (migration 0003, a partial
    unique index over `status IN ('pending','running')`) is the actual,
    database-level enforcement of invariant I5 -- see
    app/db/models/runs_repository.py for how application code cooperates
    with it rather than duplicating it.
    """

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(f"status IN {RUN_STATUSES!r}", name="ck_runs_status"),
        CheckConstraint(f"triggered_by IN {RUN_TRIGGERED_BY!r}", name="ck_runs_triggered_by"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)
    client_idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    total_tasks: Mapped[int] = mapped_column(nullable=False, server_default="0")
    succeeded_tasks: Mapped[int] = mapped_column(nullable=False, server_default="0")
    failed_tasks: Mapped[int] = mapped_column(nullable=False, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class Task(Base):
    """One (today: always exactly one, per run -- doc 18 §2.3's cardinality
    note) unit of work within a run. `idempotency_key` doubles as the
    Celery dispatch argument-of-record and the doc 07 §4/§5
    redelivery-safety key.
    """

    __tablename__ = "tasks"
    __table_args__ = (CheckConstraint(f"status IN {TASK_STATUSES!r}", name="ck_tasks_status"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(nullable=False, server_default="3")
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ObservationHistory(Base):
    """Append-only version history for a product's canonical fields (doc 18
    §2.4, adapted from doc 05 §6's `record_versions`). Enforced append-only
    by `trg_observation_history_immutable` (migration 0003) -- NOT by
    anything at this ORM layer, deliberately: the guarantee this table
    exists to provide must hold even against a direct `psql` session or a
    bug in application code, not just against callers that go through this
    class.

    Field set matches `CurrentObservation` exactly (see that model's
    docstring in scraping.py) plus `run_id`/`task_id` (provenance -- which
    scrape produced this version) and `change_summary`/`version_created_at`
    (versioning metadata). `run_id`/`task_id` stay NOT NULL -- doc 18 §2.5
    explains why no backfill row ever needs them nullable (Phase 1 history
    is not synthetically backfilled at all).
    """

    __tablename__ = "observation_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )

    product_name: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    discount: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    variant: Mapped[str | None] = mapped_column(Text, nullable=True)
    stock_status: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    review_count: Mapped[int | None] = mapped_column(nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_valid: Mapped[bool] = mapped_column(nullable=False)
    validation_errors: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(nullable=False)

    change_summary: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    version_created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("now()")
    )


# The exact field set diffed/copied between CurrentObservation and a new
# ObservationHistory row (doc 18 §3.3) -- one shared source of truth so
# repository.py's diff computation and history-row construction can never
# silently drift apart from each other.
TRACKED_OBSERVATION_FIELDS = (
    "product_name",
    "brand",
    "category",
    "price",
    "currency",
    "original_price",
    "discount",
    "variant",
    "stock_status",
    "rating",
    "review_count",
    "description",
    "image_url",
)
