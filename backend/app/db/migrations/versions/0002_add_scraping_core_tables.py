"""add scraping core tables (sources, products, current_observations)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-25

Hand-written to match 0001's style (no autogenerate tooling exists yet --
see app/db/models/base.py's docstring). Column shapes mirror the ORM
models in app/db/models/scraping.py exactly; the two are written
independently and must be kept in sync by hand until autogenerate is
introduced.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPDATED_AT_TABLES = ("sources", "products", "current_observations")


def upgrade() -> None:
    uuid_pk = sa.text("gen_random_uuid()")
    now = sa.text("now()")

    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=uuid_pk),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False, unique=True),
        sa.Column("adapter_type", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint("adapter_type IN ('mock_store')", name="ck_sources_adapter_type"),
    )

    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=uuid_pk),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id"),
            nullable=False,
        ),
        sa.Column("product_identity_key", sa.Text(), nullable=False),
        sa.Column("product_url", sa.Text(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=True),
        sa.Column("source_product_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        # Also serves as the lookup index for "all products of source X"
        # (leading-column rule) -- no separate index on source_id needed.
        sa.UniqueConstraint(
            "source_id", "product_identity_key", name="uq_products_source_identity"
        ),
    )

    op.create_table(
        "current_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=uuid_pk),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id"),
            nullable=False,
            unique=True,
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
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint(
            "stock_status IN ('in_stock', 'out_of_stock', 'preorder', 'unknown')",
            name="ck_current_observations_stock_status",
        ),
    )

    # First migration that needs an updated_at trigger (doc 05 §1's
    # convention) -- establishes the pattern for future migrations to
    # reuse (CREATE OR REPLACE, so a later migration can call this again
    # harmlessly rather than needing its own copy).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in _UPDATED_AT_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_set_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )


def downgrade() -> None:
    for table in reversed(_UPDATED_AT_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table}")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")

    # Children before parents: current_observations -> products -> sources.
    op.drop_table("current_observations")
    op.drop_table("products")
    op.drop_table("sources")
