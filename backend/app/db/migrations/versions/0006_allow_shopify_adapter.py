"""Allow 'shopify' in sources.adapter_type.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_sources_adapter_type", "sources", type_="check")
    op.create_check_constraint(
        "ck_sources_adapter_type", "sources", "adapter_type IN ('mock_store', 'shopify')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_sources_adapter_type", "sources", type_="check")
    op.create_check_constraint(
        "ck_sources_adapter_type", "sources", "adapter_type IN ('mock_store')"
    )
