"""add My Business Hub business-data foundation

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-12

Creates an isolated, workspace-owned foundation for business data:
business_data_sources, business_data_schedules, business_data_runs,
business_data_records, and append-only business_data_record_history.

This does not modify the existing web-scraping source/run/product/history
tables. It intentionally excludes API endpoints, adapters, credential
storage, upload parsing, webhook handling, automation rules, alerts, and
billing enforcement.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPDATED_AT_TABLES = (
    "business_data_sources",
    "business_data_schedules",
    "business_data_records",
)


def upgrade() -> None:
    uuid_pk = sa.text("gen_random_uuid()")
    now = sa.text("now()")

    # --- Business data sources ----------------------------------------------
    op.create_table(
        "business_data_sources",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=uuid_pk,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("adapter_type", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("data_template", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.CheckConstraint(
            "source_type IN ('file_upload', 'custom_api', 'webhook')",
            name="ck_business_data_sources_source_type",
        ),
        sa.CheckConstraint(
            "adapter_type IN ('csv', 'xlsx', 'http_json', 'incoming_json')",
            name="ck_business_data_sources_adapter_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived', 'failing')",
            name="ck_business_data_sources_status",
        ),
        sa.CheckConstraint(
            "data_template IN ('universal_table', 'product_inventory', 'sales_orders')",
            name="ck_business_data_sources_data_template",
        ),
    )
    op.create_index(
        "ix_business_data_sources_workspace_id",
        "business_data_sources",
        ["workspace_id"],
    )

    # --- Business data schedules --------------------------------------------
    op.create_table(
        "business_data_schedules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=uuid_pk,
        ),
        sa.Column(
            "business_data_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_data_sources.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "next_run_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_run_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.CheckConstraint(
            "interval_minutes BETWEEN 15 AND 43200",
            name="ck_business_data_schedules_interval_minutes",
        ),
    )
    op.create_index(
        "ix_business_data_schedules_next_run_at_active",
        "business_data_schedules",
        ["next_run_at"],
        postgresql_where=sa.text("is_active"),
    )

    # --- Business data runs -------------------------------------------------
    op.create_table(
        "business_data_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=uuid_pk,
        ),
        sa.Column(
            "business_data_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_data_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "business_data_schedule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_data_schedules.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("triggered_by", sa.Text(), nullable=False),
        sa.Column(
            "client_idempotency_key",
            sa.Text(),
            nullable=True,
            unique=True,
        ),
        sa.Column(
            "total_records",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "succeeded_records",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "failed_records",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column("error_detail", postgresql.JSONB(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "finished_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.CheckConstraint(
            "status IN ("
            "'pending', 'running', 'completed', 'completed_with_errors', "
            "'failed', 'cancelled'"
            ")",
            name="ck_business_data_runs_status",
        ),
        sa.CheckConstraint(
            "triggered_by IN ('manual', 'schedule', 'upload', 'webhook')",
            name="ck_business_data_runs_triggered_by",
        ),
    )
    op.create_index(
        "ix_business_data_runs_source_created",
        "business_data_runs",
        ["business_data_source_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_business_data_runs_in_flight",
        "business_data_runs",
        ["id"],
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_index(
        "uq_business_data_runs_one_in_flight_per_source",
        "business_data_runs",
        ["business_data_source_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    # --- Current normalized business records --------------------------------
    op.create_table(
        "business_data_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=uuid_pk,
        ),
        sa.Column(
            "business_data_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_data_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column(
            "captured_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_data_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.UniqueConstraint(
            "business_data_source_id",
            "external_id",
            name="uq_business_data_records_source_external_id",
        ),
    )
    op.create_index(
        "ix_business_data_records_source_external_id",
        "business_data_records",
        ["business_data_source_id", "external_id"],
    )

    # --- Append-only business record history --------------------------------
    op.create_table(
        "business_data_record_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=uuid_pk,
        ),
        sa.Column(
            "business_data_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_data_records.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "business_data_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_data_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("change_summary", postgresql.JSONB(), nullable=True),
        sa.Column(
            "captured_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "version_created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=now,
        ),
    )
    op.create_index(
        "ix_business_data_record_history_record_time",
        "business_data_record_history",
        ["business_data_record_id", sa.text("version_created_at DESC")],
    )

    # --- updated_at triggers ------------------------------------------------
    for table in _UPDATED_AT_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_set_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )

    # --- Append-only history, with retention-purge delete bypass -----------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_business_data_record_history_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting('app.allow_history_purge', true) = 'true' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION
                'business_data_record_history is append-only: % on id=% is not permitted',
                TG_OP,
                OLD.id;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_business_data_record_history_immutable
        BEFORE UPDATE OR DELETE ON business_data_record_history
        FOR EACH ROW EXECUTE FUNCTION reject_business_data_record_history_mutation();
        """
    )

def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_business_data_record_history_immutable "
        "ON business_data_record_history"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_business_data_record_history_mutation()")

    for table in reversed(_UPDATED_AT_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table}")

    # Drop children before parents because of foreign-key dependencies.
    op.drop_table("business_data_record_history")
    op.drop_table("business_data_records")
    op.drop_table("business_data_runs")
    op.drop_table("business_data_schedules")
    op.drop_table("business_data_sources")
