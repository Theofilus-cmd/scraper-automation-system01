"""Shared types for the adapter pipeline (doc 08 §1/§3).

`RawFields` is intentionally loosely typed -- every value is a raw string
straight off the page, uninterpreted. `NormalizedRecord` is the strongly
typed, validated shape everything downstream of `normalize()` works with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TypedDict


class RawFields(TypedDict, total=False):
    """Loosely-typed extraction output -- one dict per product found on the
    page (doc 08 §1). Every value is still a raw string, e.g. price might be
    "$39.99" or "39,99 EUR" -- normalize() does all the interpretation.

    `price_currency` is a deliberate, minimal addition beyond doc 08 §1's
    literal field list: §3's currency rule requires "explicit source value
    if present", but the literal RawFields shape has no slot for one. JSON-LD
    (schema.org `offers.priceCurrency`) provides exactly this signal
    cleanly, so adapters that have it should populate it; normalize() still
    falls back to symbol-inference from `price` when it's absent, per §3's
    documented fallback chain. Flagged here and in doc 17 rather than
    silently added.
    """

    product_name: str | None
    brand: str | None
    category: str | None
    price: str | None
    price_currency: str | None
    original_price: str | None
    discount: str | None
    sku: str | None
    product_id: str | None
    variant: str | None
    stock_status_raw: str | None
    rating: str | None
    review_count: str | None
    description: str | None
    image_url: str | None


@dataclass(frozen=True, slots=True)
class RawPage:
    """What a `fetch()` call returns -- the raw page plus request metadata."""

    html: str
    final_url: str
    status_code: int
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class SiteSniff:
    """Cheap pre-fetch signal shared across all adapters' matches() calls
    (doc 08 §2 -- "a HEAD request plus a capped read of the first ~50KB").
    Phase 1's single adapter matches purely on hostname and never inspects
    page content, so this carries nothing yet; kept as a real type (not
    `None`) so the `matches(url, sniff)` signature doc 08 §1 specifies
    doesn't need to change when a content-sniffing adapter arrives later.
    """


@dataclass(frozen=True, slots=True)
class FetchContext:
    """Placeholder for future per-fetch configuration (proxy selection,
    session reuse, etc. -- doc 08 §1's `fetch(url, ctx)` signature). Empty
    in Phase 1; the fetcher does not consume it yet.
    """


@dataclass(frozen=True, slots=True)
class ExtractionSchema:
    """Which fields an adapter's parse() should attempt (doc 08 §1's
    `schema.enabled_fields`). Phase 1 has no `extraction_schemas` table, so
    callers use `ExtractionSchema.all_fields()` -- every adapter attempts
    every field it can find, nothing is filtered out yet.
    """

    enabled_fields: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def all_fields(cls) -> ExtractionSchema:
        return cls(enabled_fields=frozenset(RawFields.__annotations__.keys()))


@dataclass(slots=True)
class NormalizedRecord:
    """The canonical 17-field schema (doc 08 §3), after normalize().

    Only `product_url` and `scraped_at` are guaranteed non-None by
    construction (the system sets them regardless of parse outcome) --
    `stock_status` is likewise never None (unparseable maps to "unknown",
    doc 08 §3, never guessed as in-stock). Every other field may be `None`
    if the source didn't have it or it failed to parse; `validate()`
    (app/domain/validation.py) is what actually enforces which of these
    being None constitutes failure vs. an acceptable partial success.
    """

    product_url: str
    scraped_at: datetime
    stock_status: str

    product_name: str | None = None
    brand: str | None = None
    category: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    original_price: Decimal | None = None
    discount: Decimal | None = None
    sku: str | None = None
    product_id: str | None = None
    variant: str | None = None
    rating: Decimal | None = None
    review_count: int | None = None
    description: str | None = None
    image_url: str | None = None

    # Not part of doc 08 §3's schema -- records which *optional* fields had
    # a raw value present that failed to parse, so validate() can surface
    # that detail (doc 08 §4's "partial success" case) without re-deriving
    # it. Required-field problems are NOT recorded here (validate() checks
    # those directly on the typed fields above).
    parse_warnings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """doc 08 §4's outcome, computed by app/domain/validation.py.

    `is_valid=True` with a non-empty `errors` is the "partial success" row
    of doc 08 §4's table (all required fields fine, some optional field
    didn't parse). `is_valid=False` is the "failure" row -- the caller must
    not write/update `current_observations` when this is False (repository
    layer enforces this, not left to callers to remember).
    """

    is_valid: bool
    errors: dict[str, str]
