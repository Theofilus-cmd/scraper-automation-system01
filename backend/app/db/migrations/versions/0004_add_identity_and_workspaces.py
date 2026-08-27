"""add identities, workspaces, and source tenancy

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-27

Creates the identity and workspace tenancy foundation and moves all
pre-authentication sources into a disabled system-owned legacy workspace.

Existing sources remain intact: each receives the legacy workspace ID.
Runs, tasks, products, observations, schedules, and history inherit that
ownership through their existing source/run relationships.

The former globally unique active-source URL index is replaced with a
workspace-scoped partial unique index. Different customers can therefore
monitor the same URL, while one workspace cannot create duplicate active
sources for the same normalized URL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_USER_EMAIL = "legacy-owner@local.invalid"

LEGACY_USER_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$pmk41EnHEyqMVikk+lYyRw$X+kpRnApgsvxi5K1Mkfm736bsmslClPI+wfrnBIgu/A"
LEGACY_WORKSPACE_SLUG = "legacy-workspace"


def upgrade() -> None:
    uuid_pk = sa.text("gen_random_uuid()")
    now = sa.text("now()")

    # --- Identity ------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=uuid_pk,
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "workspaces",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=uuid_pk,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )

    op.create_table(
        "workspace_members",
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'viewer')",
            name="ck_workspace_members_role",
        ),
    )
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    for table in ("users", "workspaces"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_set_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )

    # --- Legacy ownership ----------------------------------------------------
    # CTEs make the generated UUIDs explicit and reuse them safely for the
    # workspace and membership rows. The account is inactive, so it cannot
    # authenticate; its random Argon2-looking value is never a usable secret.
    op.execute(
        sa.text(
            """
            WITH legacy_user AS (
                INSERT INTO users (
                    email, password_hash, display_name, is_active, is_verified
                )
                VALUES (
                    :email, :password_hash, 'Legacy System Owner', false, false
                )
                RETURNING id
            ),
            legacy_workspace AS (
                INSERT INTO workspaces (name, slug, owner_user_id)
                SELECT 'Legacy Workspace', :slug, id
                FROM legacy_user
                RETURNING id, owner_user_id
            )
            INSERT INTO workspace_members (workspace_id, user_id, role)
            SELECT id, owner_user_id, 'owner'
            FROM legacy_workspace
            """
        ).bindparams(
            email=LEGACY_USER_EMAIL,
            password_hash=LEGACY_USER_PASSWORD_HASH,
            slug=LEGACY_WORKSPACE_SLUG,
        )
    )

    # --- Source tenancy ------------------------------------------------------
    # Nullable only while historic rows are backfilled.
    op.add_column(
        "sources",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    op.execute(
        sa.text(
            """
            UPDATE sources
            SET workspace_id = (
                SELECT id FROM workspaces WHERE slug = :slug
            )
            WHERE workspace_id IS NULL
            """
        ).bindparams(slug=LEGACY_WORKSPACE_SLUG)
    )

    op.alter_column("sources", "workspace_id", nullable=False)
    op.create_foreign_key(
        "fk_sources_workspace_id_workspaces",
        "sources",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_sources_workspace_id", "sources", ["workspace_id"])

    # Migration 0003's global partial index blocks the same source URL in
    # different workspaces. Replace it with a tenant-scoped equivalent.
    op.drop_index("uq_sources_normalized_url_active", table_name="sources")
    op.create_index(
        "uq_sources_workspace_normalized_url_active",
        "sources",
        ["workspace_id", "normalized_url"],
        unique=True,
        postgresql_where=sa.text("status != 'archived'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sources_workspace_normalized_url_active", table_name="sources")
    op.create_index(
        "uq_sources_normalized_url_active",
        "sources",
        ["normalized_url"],
        unique=True,
        postgresql_where=sa.text("status != 'archived'"),
    )

    op.drop_index("ix_sources_workspace_id", table_name="sources")
    op.drop_constraint("fk_sources_workspace_id_workspaces", "sources", type_="foreignkey")
    op.drop_column("sources", "workspace_id")

    for table in ("workspaces", "users"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table}")

    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
    op.drop_table("users")