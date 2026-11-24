"""ORM models for Phase 4 product intelligence (migration `0008`)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    ForeignKey,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

INTERNAL_PRODUCT_STATUSES = ("active", "archived")
MATCH_TYPES = ("sku", "url", "manual")


class InternalProduct(Base):
    """A product in the workspace's own catalog."""

    __tablename__ = "internal_products"
    __table_args__ = (
        CheckConstraint("own_price >= 0", name="ck_internal_products_own_price"),
        CheckConstraint(
            "map_price IS NULL OR map_price >= 0", name="ck_internal_products_map_price"
        ),
        CheckConstraint(
            f"status IN {INTERNAL_PRODUCT_STATUSES!r}", name="ck_internal_products_status"
        ),
        CheckConstraint("currency = upper(currency)", name="ck_internal_products_currency"),
        # Case-insensitive per-workspace SKU uniqueness is a functional partial
        # index created in migration 0008 (uq_internal_products_workspace_sku).
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sku: Mapped[str | None] = mapped_column(Text, nullable=True)
    barcode: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    own_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    map_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class ProductMatch(Base):
    """Link between an internal product and a scraped competitor product."""

    __tablename__ = "product_matches"
    __table_args__ = (
        CheckConstraint(f"match_type IN {MATCH_TYPES!r}", name="ck_product_matches_type"),
        UniqueConstraint("internal_product_id", "product_id", name="uq_product_matches_pair"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    internal_product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("internal_products.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    match_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
