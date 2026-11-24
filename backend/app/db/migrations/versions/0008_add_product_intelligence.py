"""add product intelligence core: internal_products, product_matches

Revision ID: 0008
Revises: 0007

`internal_products` is the workspace's own catalog. `product_matches` links
an internal product to a scraped competitor `products` row. SKU is unique per
workspace (case-insensitive). A match is unique per (internal, competitor).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "internal_products",
        _uuid_pk(),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=True),
        sa.Column("barcode", sa.Text(), nullable=True),
        sa.Column("brand", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("own_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("map_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        *_timestamps(),
        sa.CheckConstraint("own_price >= 0", name="ck_internal_products_own_price"),
        sa.CheckConstraint(
            "map_price IS NULL OR map_price >= 0", name="ck_internal_products_map_price"
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_internal_products_status"),
        sa.CheckConstraint("currency = upper(currency)", name="ck_internal_products_currency"),
    )
    op.create_index(
        "uq_internal_products_workspace_sku",
        "internal_products",
        ["workspace_id", sa.text("lower(sku)")],
        unique=True,
        postgresql_where=sa.text("sku IS NOT NULL"),
    )
    op.create_index(
        "ix_internal_products_workspace_created",
        "internal_products",
        ["workspace_id", "created_at"],
    )

    op.create_table(
        "product_matches",
        _uuid_pk(),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "internal_product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("internal_products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("match_type", sa.Text(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "match_type IN ('sku', 'url', 'manual')", name="ck_product_matches_type"
        ),
        sa.UniqueConstraint("internal_product_id", "product_id", name="uq_product_matches_pair"),
    )
    op.create_index(
        "ix_product_matches_workspace_created", "product_matches", ["workspace_id", "created_at"]
    )
    op.create_index("ix_product_matches_product_id", "product_matches", ["product_id"])

    for table in ("internal_products", "product_matches"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_set_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )


def downgrade() -> None:
    for table in ("product_matches", "internal_products"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table}")
    op.drop_table("product_matches")
    op.drop_table("internal_products")
