"""add product alerts (price change / stock status change)

Revision ID: 0007
Revises: 0006

`alerts` rows are written in the same transaction as the observation write
that caused them. `dedupe_key` is UNIQUE so a retried/redelivered task can
never create a second alert for the same change. Email delivery state lives
on the row (`email_status`, `attempts`, `claimed_at`) so delivery is
claim-then-send and safe under concurrent workers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("product_name", sa.Text(), nullable=False),
        sa.Column("product_url", sa.Text(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=False),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("email_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('price_changed', 'stock_status_changed')", name="ck_alerts_kind"
        ),
        sa.CheckConstraint(
            "email_status IN ('pending', 'sending', 'sent', 'failed', 'skipped')",
            name="ck_alerts_email_status",
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_alerts_dedupe_key"),
    )
    op.create_index("ix_alerts_workspace_created", "alerts", ["workspace_id", "created_at"])
    op.create_index(
        "ix_alerts_deliverable",
        "alerts",
        ["created_at"],
        postgresql_where=sa.text("email_status IN ('pending', 'sending')"),
    )


def downgrade() -> None:
    op.drop_index("ix_alerts_deliverable", table_name="alerts")
    op.drop_index("ix_alerts_workspace_created", table_name="alerts")
    op.drop_table("alerts")
