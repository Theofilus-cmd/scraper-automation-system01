"""MockStoreAdapter -- the one adapter Phase 1 ships (doc 17 Scope
Decision 1). Targets only the local `mock-store` service; matching is a
pure hostname check, never a network call.

`parse()` extracts the page's schema.org JSON-LD `Product` block(s) and
maps schema.org keys onto `RawFields` keys 1:1, with no interpretation --
notably, `offers.availability` (e.g. `"https://schema.org/InStock"`) is
copied verbatim into `stock_status_raw` and is *not* mapped to the 4-value
stock_status enum here. That mapping is `app.scraping.normalize`'s
`_map_stock_status()`'s job (doc 08 §1: adapter-specific extraction and
adapter-agnostic interpretation stay strictly separated).
"""

from __future__ import annotations

import json
from typing import ClassVar
from urllib.parse import urlparse

from selectolax.parser import HTMLParser  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.core.logging import get_logger
from app.scraping import fetcher
from app.scraping.types import ExtractionSchema, FetchContext, RawFields, RawPage, SiteSniff

logger = get_logger(__name__)

_JSON_LD_SELECTOR = 'script[type="application/ld+json"]'


def _as_str(value: object) -> str | None:
    """Coerce one JSON-LD scalar into RawFields's uniform `str | None`
    shape. Anything not a plain scalar (nested dict/list we don't expect
    here) is dropped rather than stringified, since that would produce
    garbage like a Python repr for normalize() to trip over.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return None


def _first_if_list(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _parse_product(data: dict[str, object]) -> RawFields:
    offers = _first_if_list(data.get("offers"))
    offers_dict = offers if isinstance(offers, dict) else {}

    rating = data.get("aggregateRating")
    rating_dict = rating if isinstance(rating, dict) else {}

    brand = data.get("brand")
    brand_name = brand.get("name") if isinstance(brand, dict) else brand

    image = _first_if_list(data.get("image"))

    return RawFields(
        product_name=_as_str(data.get("name")),
        brand=_as_str(brand_name),
        category=_as_str(data.get("category")),
        price=_as_str(offers_dict.get("price")),
        price_currency=_as_str(offers_dict.get("priceCurrency")),
        sku=_as_str(data.get("sku")),
        product_id=_as_str(data.get("productID")),
        stock_status_raw=_as_str(offers_dict.get("availability")),
        rating=_as_str(rating_dict.get("ratingValue")),
        review_count=_as_str(rating_dict.get("reviewCount")),
        description=_as_str(data.get("description")),
        image_url=_as_str(image),
    )


class MockStoreAdapter:
    """doc 08 §1's `SourceAdapter` contract, implemented for `mock-store`."""

    slug: ClassVar[str] = "mock_store"
    requires_js: ClassVar[bool] = False

    def matches(self, url: str, sniff: SiteSniff) -> float:
        settings = get_settings()
        target_host = urlparse(settings.mock_store_base_url).hostname
        return 1.0 if target_host and urlparse(url).hostname == target_host else 0.0

    async def fetch(self, url: str, ctx: FetchContext | None = None) -> RawPage:
        return await fetcher.fetch(url)

    def parse(self, page: RawPage, schema: ExtractionSchema | None = None) -> list[RawFields]:
        """Find every `<script type="application/ld+json">` block, parse
        each as JSON (dict or list-of-dicts), and keep only items whose
        `@type` is `"Product"`. Malformed JSON-LD blocks are logged and
        skipped rather than failing the whole page -- one bad block
        shouldn't hide products found in good ones.
        """
        tree = HTMLParser(page.html)
        results: list[RawFields] = []

        for node in tree.css(_JSON_LD_SELECTOR):
            text = node.text(strip=True)
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("mock_store adapter: skipping malformed JSON-LD block")
                continue

            candidates = parsed if isinstance(parsed, list) else [parsed]
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("@type") == "Product":
                    results.append(_parse_product(candidate))

        return results
