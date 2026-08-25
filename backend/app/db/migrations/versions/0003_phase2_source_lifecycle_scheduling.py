"""add phase 2 source lifecycle, scheduling, runs/tasks, observation history

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-25

Hand-written, matching 0001/0002's style (no autogenerate tooling exists
yet -- see 0002's docstring). Implements doc 18 §2 in full: adds
`sources.status`; creates `schedules`, `runs`, `tasks`, `observation_history`;
adds the no-overlap-per-source partial unique index on `runs` (doc 18 §4.4);
adds the `observation_history` immutability trigger with its retention-purge
bypass (doc 18 §2.4, §7.5). Column shapes mirror app/db/models/lifecycle.py
and the extended Source model in app/db/models/scraping.py exactly -- kept
in sync by hand, same convention 0002 already established.

One correction to doc 18's literal schema, made here and flagged plainly
(see _drop_normalized_url_unique_constraint()'s docstring below): doc 18
§6.1 requires `POST /sources` to be able to create a NEW row with the same
`normalized_url` as an already-archived one ("no match, or the only match
is archived -> 201 with a new row"). Migration 0002's plain
`UNIQUE(normalized_url)` constraint makes that impossible -- it was never
exercised by Phase 1 (no archive concept existed yet), so the conflict
went unnoticed during design review. Fixed here the same way doc 18
already fixes the analogous "at most one in-flight run" problem: replace
the plain unique constraint with a partial unique index that only applies
to non-archived rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_UPDATED_AT_TABLES = ("schedules",)


def _drop_normalized_url_unique_constraint() -> None:
    """Removes migration 0002's plain `UNIQUE(normalized_url)` constraint,
    replaced below by `uq_sources_normalized_url_active` (partial, excludes
    archived rows). Looked up via live introspection rather than assumed by
    name (Postgres's default auto-generated name for an unnamed
    single-column UNIQUE from `Column(unique=True)` is conventionally
    `sources_normalized_url_key`, but this asks the database directly
    instead of trusting that convention blind -- this migration cannot be
    exercised against a real Postgres from this delivery's sandbox, so it
    fails loudly with a clear message instead of guessing if the
    constraint isn't where expected).
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for uc in inspector.get_unique_constraints("sources"):
        if uc["column_names"] == ["normalized_url"]:
            op.drop_constraint(uc["name"], "sources", type_="unique")
            return
    raise RuntimeError(
        "expected a unique constraint on sources.normalized_url from "
        "migration 0002 but found none -- migration 0003 cannot safely "
        "replace it with the partial unique index without knowing its "
        "exact name. Inspect `sources` manually before re-running."
    )


def upgrade() -> None:
    uuid_pk = sa.text("gen_random_uuid()")
    now = sa.text("now()")

    # --- 2.1 sources: extended, not replaced --------------------------------
    op.add_column(
        "sources",
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
    )
    op.create_check_constraint(
        "ck_sources_status", "sources", "status IN ('active', 'paused', 'archived')"
    )
    op.create_index(
        "ix_sources_status_active",
        "sources",
        ["id"],
        postgresql_where=sa.text("status = 'active'"),
    )

    _drop_normalized_url_unique_constraint()
    op.create_index(
        "uq_sources_normalized_url_active",
        "sources",
        ["normalized_url"],
        unique=True,
        postgresql_where=sa.text("status != 'archived'"),
    )

    # --- 2.2 schedules -------------------------------------------------------
    op.create_table(
        "schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=uuid_pk),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint(
            "interval_minutes BETWEEN 15 AND 10080", name="ck_schedules_interval_minutes"
        ),
    )
    op.create_index(
        "ix_schedules_next_run_at_active",
        "schedules",
        ["next_run_at"],
        postgresql_where=sa.text("is_active"),
    )

    # --- 2.3 runs / tasks, incl. the no-overlap partial unique index --------
    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=uuid_pk),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "schedule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("schedules.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("triggered_by", sa.Text(), nullable=False),
        sa.Column("client_idempotency_key", sa.Text(), nullable=True, unique=True),
        sa.Column("total_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','completed_with_errors',"
            "'failed','cancelled')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint("triggered_by IN ('schedule','manual')", name="ck_runs_triggered_by"),
    )
    op.create_index("ix_runs_source_created", "runs", ["source_id", sa.text("created_at DESC")])
    op.create_index(
        "ix_runs_in_flight",
        "runs",
        ["id"],
        postgresql_where=sa.text("status IN ('pending','running')"),
    )
    # The no-overlap invariant's actual enforcement mechanism (doc 18 §4.4) --
    # a race-free, database-level guarantee, not an application-level check.
    op.create_index(
        "uq_runs_one_in_flight_per_source",
        "runs",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','running')"),
    )

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=uuid_pk),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column("error_detail", postgresql.JSONB(), nullable=True),
        sa.Column("queued_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','in_progress','succeeded','failed','retrying','dead_letter')",
            name="ck_tasks_status",
        ),
    )
    op.create_index("ix_tasks_run", "tasks", ["run_id"])
    op.create_index(
        "ix_tasks_stuck",
        "tasks",
        ["id"],
        postgresql_where=sa.text("status IN ('queued','retrying')"),
    )

    # --- 2.4 observation_history ----------------------------------------------
    op.create_table(
        "observation_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=uuid_pk),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("product_name", sa.Text(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("original_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("discount", sa.Numeric(6, 2), nullable=True),
        sa.Column("variant", sa.Text(), nullable=True),
        sa.Column("stock_status", sa.Text(), nullable=False),
        sa.Column("rating", sa.Numeric(3, 2), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=True),
        sa.Column("scraped_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("change_summary", postgresql.JSONB(), nullable=True),
        sa.Column(
            "version_created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now
        ),
    )
    op.create_index(
        "ix_observation_history_product_time",
        "observation_history",
        ["product_id", sa.text("version_created_at DESC")],
    )

    # --- set_updated_at() trigger for the one new table that needs it ---------
    for table in _NEW_UPDATED_AT_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_set_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )

    # --- observation_history immutability, with a narrow retention-purge bypass
    # (doc 18 §2.4/§7.5): UPDATE is unconditionally rejected, always. DELETE
    # is rejected UNLESS the deleting transaction has explicitly set
    # app.allow_history_purge='true' via SET LOCAL, scoped to exactly that
    # transaction (SET LOCAL reverts automatically at transaction end, so it
    # can never leak onto a later query on a pooled PgBouncer connection).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_observation_history_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_setting('app.allow_history_purge', true) = 'true' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'observation_history is append-only: % on id=% is not permitted',
                TG_OP, OLD.id;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_observation_history_immutable
        BEFORE UPDATE OR DELETE ON observation_history
        FOR EACH ROW EXECUTE FUNCTION reject_observation_history_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_observation_history_immutable ON observation_history")
    op.execute("DROP FUNCTION IF EXISTS reject_observation_history_mutation()")

    for table in reversed(_NEW_UPDATED_AT_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table}")

    # Children before parents: observation_history -> tasks -> runs ->
    # schedules -> sources.status (same FK-driven ordering as §2.5/§2.6).
    op.drop_table("observation_history")
    op.drop_table("tasks")
    op.drop_table("runs")
    op.drop_table("schedules")

    op.drop_index("uq_sources_normalized_url_active", table_name="sources")
    op.create_unique_constraint("sources_normalized_url_key", "sources", ["normalized_url"])

    op.drop_index("ix_sources_status_active", table_name="sources")
    op.drop_constraint("ck_sources_status", "sources", type_="check")
    op.drop_column("sources", "status")
