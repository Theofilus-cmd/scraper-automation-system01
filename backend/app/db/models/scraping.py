"""ORM models for Phase 1's scraping core tables (migration `0002`).

Deliberately simpler than doc 05 §6's full `records`/`targets` model -- no
`workspace_id`, no `project_id`, no history table (doc 17 Scope Decision
2). Column shapes mirror doc 05 §1's conventions (uuid pk via pgcrypto's
`gen_random_uuid()`, timestamptz `created_at`/`updated_at` with a
DB-level `set_updated_at()` trigger -- see migration `0002` -- CHECK
constraints over text instead of native enums) and doc 05 §6's `records`
table for field types (`numeric(12,2)` prices, `char(3)` currency,
`numeric(3,2)` rating, `numeric(6,2)` discount).

One deliberate divergence from doc 05 §6's general `records` table:
`product_name`/`price`/`currency`/`stock_status` are NOT NULL here, where
doc 05's table lists them nullable. Phase 1's write-gating rule (doc 08
§4, enforced in `repository.py::upsert_scrape_result()`) guarantees a
`current_observations` row is only ever created or updated by a validated
write, so these fields are always populated whenever the row exists at
all -- this is a stricter, Phase-1-specific choice, not a schema bug.

`sku`/`source_product_id`/`product_url` live once, on `products` (they
double as `product_identity_key` inputs -- see `repository.py`) rather
than being duplicated onto `current_observations`.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models.identity import Workspace

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CHAR, CheckConstraint, ForeignKey, Index, Numeric, Text, UniqueConstraint, text

from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base

# doc 18 §3.1's source lifecycle -- kept here (not in lifecycle.py) since it
# lives directly on the Source model this file already owns.
SOURCE_STATUSES = ("active", "paused", "archived")


class Source(Base):
    """One scrape target's origin site -- deliberately simpler than doc 05
    §4's `targets`: no project/workspace/schedule (Phase 1 has none of
    those).

    `status` (doc 18 §2.1, migration `0003`) is the only Phase 2 addition to
    this table. Note `normalized_url` is UNIQUE only among non-archived rows
    as of migration `0003` (`uq_sources_normalized_url_active`, a partial
    index) -- the plain `unique=True` below still documents intent at the
    ORM/Python level (SQLAlchemy does not model partial uniqueness through
    a `Mapped[]` flag), but the actual DB-level constraint enforcing it is
    the partial index, not this column flag. See migration 0003's docstring
    for why: an archived source and a freshly-created one may legitimately
    share a normalized_url (doc 18 §6.1's re-add-after-archive behavior).
    """

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("adapter_type IN ('mock_store')", name="ck_sources_adapter_type"),
        CheckConstraint(f"status IN {SOURCE_STATUSES!r}", name="ck_sources_status"),
    )
    Index("ix_sources_workspace_id", "workspace_id"),

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)

    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    adapter_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    workspace: Mapped["Workspace"] = relationship(back_populates="sources")
    products: Mapped[list[Product]] = relationship(back_populates="source")


class Product(Base):
    """One product identity at one source (doc 05 §6 / doc 10 §1's
    `product_identity_key` precedence: `sku`, else `source_product_id`,
    else normalized `product_url` -- computed in `repository.py`, stored
    here rather than recomputed on every read).
    """

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("source_id", "product_identity_key", name="uq_products_source_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id"), nullable=False
    )
    product_identity_key: Mapped[str] = mapped_column(Text, nullable=False)
    product_url: Mapped[str] = mapped_column(Text, nullable=False)
    sku: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_product_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    source: Mapped[Source] = relationship(back_populates="products")
    current_observation: Mapped[CurrentObservation | None] = relationship(
        back_populates="product", uselist=False
    )


class CurrentObservation(Base):
    """The single current snapshot for a product (doc 08 §3's canonical
    fields, minus the identity fields that live on `Product` -- see module
    docstring). One row per product, enforced by `product_id`'s
    `unique=True`; `repository.py::upsert_scrape_result()` is the only
    writer, and only ever on a validated (full or partial success) scrape.
    """

    __tablename__ = "current_observations"
    __table_args__ = (
        CheckConstraint(
            "stock_status IN ('in_stock', 'out_of_stock', 'preorder', 'unknown')",
            name="ck_current_observations_stock_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False, unique=True
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

    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    product: Mapped[Product] = relationship(back_populates="current_observation")
