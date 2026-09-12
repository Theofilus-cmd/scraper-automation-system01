"""ORM models for My Business Hub's scheduled business-data foundation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

if TYPE_CHECKING:
    from app.db.models.identity import Workspace


BUSINESS_DATA_SOURCE_TYPES = ("file_upload", "custom_api", "webhook")
BUSINESS_DATA_ADAPTER_TYPES = ("csv", "xlsx", "http_json", "incoming_json")
BUSINESS_DATA_SOURCE_STATUSES = ("draft", "active", "paused", "archived", "failing")
BUSINESS_DATA_TEMPLATES = ("universal_table", "product_inventory", "sales_orders")

BUSINESS_DATA_SCHEDULE_MIN_INTERVAL_MINUTES = 15
BUSINESS_DATA_SCHEDULE_MAX_INTERVAL_MINUTES = 43_200

BUSINESS_DATA_RUN_STATUSES = (
    "pending",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
    "cancelled",
)
BUSINESS_DATA_RUN_IN_FLIGHT_STATUSES = ("pending", "running")
BUSINESS_DATA_RUN_TRIGGERED_BY = ("manual", "schedule", "upload", "webhook")


class BusinessDataSource(Base):
    """One workspace-owned source of business data for My Business Hub."""

    __tablename__ = "business_data_sources"
    __table_args__ = (
        CheckConstraint(
            f"source_type IN {BUSINESS_DATA_SOURCE_TYPES!r}",
            name="ck_business_data_sources_source_type",
        ),
        CheckConstraint(
            f"adapter_type IN {BUSINESS_DATA_ADAPTER_TYPES!r}",
            name="ck_business_data_sources_adapter_type",
        ),
        CheckConstraint(
            f"status IN {BUSINESS_DATA_SOURCE_STATUSES!r}",
            name="ck_business_data_sources_status",
        ),
        CheckConstraint(
            f"data_template IN {BUSINESS_DATA_TEMPLATES!r}",
            name="ck_business_data_sources_data_template",
        ),
        Index("ix_business_data_sources_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    adapter_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="draft",
    )
    data_template: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("now()"),
    )


class BusinessDataSchedule(Base):
    """At most one scheduled sync configuration for a business data source."""

    __tablename__ = "business_data_schedules"
    __table_args__ = (
        CheckConstraint(
            f"interval_minutes BETWEEN {BUSINESS_DATA_SCHEDULE_MIN_INTERVAL_MINUTES} "
            f"AND {BUSINESS_DATA_SCHEDULE_MAX_INTERVAL_MINUTES}",
            name="ck_business_data_schedules_interval_minutes",
        ),
        Index(
            "ix_business_data_schedules_next_run_at_active",
            "next_run_at",
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_data_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business_data_sources.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    interval_minutes: Mapped[int] = mapped_column(nullable=False)
    is_active: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("true"),
    )
    next_run_at: Mapped[datetime] = mapped_column(nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("now()"),
    )


class BusinessDataRun(Base):
    """One manual, scheduled, uploaded-file, or webhook processing run."""

    __tablename__ = "business_data_runs"
    __table_args__ = (
        CheckConstraint(
            f"status IN {BUSINESS_DATA_RUN_STATUSES!r}",
            name="ck_business_data_runs_status",
        ),
        CheckConstraint(
            f"triggered_by IN {BUSINESS_DATA_RUN_TRIGGERED_BY!r}",
            name="ck_business_data_runs_triggered_by",
        ),
        Index(
            "ix_business_data_runs_source_created",
            "business_data_source_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_business_data_runs_in_flight",
            "id",
            postgresql_where=text(
                "status IN ('pending', 'running')",
            ),
        ),
        Index(
            "uq_business_data_runs_one_in_flight_per_source",
            "business_data_source_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'running')",
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_data_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business_data_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    business_data_schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business_data_schedules.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)
    client_idempotency_key: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        unique=True,
    )
    total_records: Mapped[int] = mapped_column(
        nullable=False,
        server_default="0",
    )
    succeeded_records: Mapped[int] = mapped_column(
        nullable=False,
        server_default="0",
    )
    failed_records: Mapped[int] = mapped_column(
        nullable=False,
        server_default="0",
    )
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[dict[str, object] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("now()"),
    )


class BusinessDataRecord(Base):
    """The newest normalized value for one external record in one source."""

    __tablename__ = "business_data_records"
    __table_args__ = (
        UniqueConstraint(
            "business_data_source_id",
            "external_id",
            name="uq_business_data_records_source_external_id",
        ),
        Index(
            "ix_business_data_records_source_external_id",
            "business_data_source_id",
            "external_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_data_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business_data_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    fields: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
    )
    captured_at: Mapped[datetime] = mapped_column(nullable=False)
    last_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business_data_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("now()"),
    )


class BusinessDataRecordHistory(Base):
    """Append-only snapshot history for a normalized business data record."""

    __tablename__ = "business_data_record_history"
    __table_args__ = (
        Index(
            "ix_business_data_record_history_record_time",
            "business_data_record_id",
            text("version_created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_data_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business_data_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    business_data_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business_data_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    fields: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
    )
    change_summary: Mapped[dict[str, object] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    captured_at: Mapped[datetime] = mapped_column(nullable=False)
    version_created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("now()"),
    )
