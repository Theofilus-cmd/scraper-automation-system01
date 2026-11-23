"""ShopifyAdapter -- reads public Shopify storefronts via the unauthenticated
`/products.json` / `/products/<handle>.json` endpoints, falling back to
schema.org JSON-LD when the page is HTML.

Matching is path-based (pure, no I/O): Shopify URLs have a recognizable
shape (`/products/<handle>`, `/collections/<c>`, `/products.json`) but the
host can be any custom domain, so confidence stays below the mock-store's
1.0 and is only ever a heuristic.

`parse()` extracts raw strings only; interpretation stays in `normalize()`.
Shopify's `available` boolean is mapped to the literals "InStock" /
"OutOfStock" (the vocabulary normalize already understands) -- the only
translation done here, because the raw value is a bool, not a string.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar
from urllib.parse import urlparse, urlunparse

from app.core.logging import get_logger
from app.scraping import fetcher
from app.scraping.adapters.mock_store import MockStoreAdapter
from app.scraping.types import ExtractionSchema, FetchContext, RawFields, RawPage, SiteSniff

logger = get_logger(__name__)

_PRODUCT_PATH = re.compile(r"^(?:/collections/[^/]+)?/products/([^/]+?)(?:\.json|\.js)?/?$")
_PRODUCTS_JSON_PATH = re.compile(r"^/(?:collections/[^/]+/)?products\.json/?$")
_COLLECTION_PATH = re.compile(r"^/collections/[^/]+/?$")
_TAG_RE = re.compile(r"<[^>]+>")

PAGE_LIMIT = 250  # Shopify's documented maximum for products.json


def _as_str(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return None


def _json_url(url: str) -> str:
    """Map a storefront URL onto its public JSON endpoint."""
    parts = urlparse(url)
    path = parts.path
    m = _PRODUCT_PATH.match(path)
    if m:
        return urlunparse(parts._replace(path=f"/products/{m.group(1)}.json", query=""))
    if _PRODUCTS_JSON_PATH.match(path):
        return urlunparse(parts._replace(query=parts.query or f"limit={PAGE_LIMIT}"))
    if _COLLECTION_PATH.match(path):
        return urlunparse(
            parts._replace(path=path.rstrip("/") + "/products.json", query=f"limit={PAGE_LIMIT}")
        )
    return url


def _parse_product(data: dict[str, object]) -> RawFields:
    variants = data.get("variants")
    variant = variants[0] if isinstance(variants, list) and variants else {}
    if not isinstance(variant, dict):
        variant = {}

    image = data.get("image")
    image_src = image.get("src") if isinstance(image, dict) else None
    if image_src is None:
        images = data.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            image_src = images[0].get("src")

    body = _as_str(data.get("body_html"))
    available = variant.get("available")
    stock_raw = None
    if isinstance(available, bool):
        stock_raw = "InStock" if available else "OutOfStock"

    variant_title = _as_str(variant.get("title"))
    return RawFields(
        product_name=_as_str(data.get("title")),
        brand=_as_str(data.get("vendor")),
        category=_as_str(data.get("product_type")) or None,
        price=_as_str(variant.get("price")),
        original_price=_as_str(variant.get("compare_at_price")),
        sku=_as_str(variant.get("sku")) or None,
        product_id=_as_str(data.get("id")),
        variant=None if variant_title == "Default Title" else variant_title,
        stock_status_raw=stock_raw,
        description=_TAG_RE.sub("", body).strip() if body else None,
        image_url=_as_str(image_src),
    )


class ShopifyAdapter:
    slug: ClassVar[str] = "shopify"
    requires_js: ClassVar[bool] = False

    def matches(self, url: str, sniff: SiteSniff) -> float:
        path = urlparse(url).path
        if _PRODUCTS_JSON_PATH.match(path) or _PRODUCT_PATH.match(path):
            return 0.6
        if _COLLECTION_PATH.match(path):
            return 0.5
        return 0.0

    async def fetch(self, url: str, ctx: FetchContext | None = None) -> RawPage:
        page = await fetcher.fetch(_json_url(url))
        if page.status_code == 200 and page.html.lstrip().startswith(("{", "[")):
            return page
        # Endpoint disabled/blocked: fall back to the human-facing page so
        # parse() can try JSON-LD.
        if _json_url(url) != url:
            return await fetcher.fetch(url)
        return page

    def parse(self, page: RawPage, schema: ExtractionSchema | None = None) -> list[RawFields]:
        text = page.html.lstrip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("shopify adapter: malformed JSON response")
                return []
            if not isinstance(data, dict):
                return []
            if isinstance(data.get("products"), list):
                items = data["products"]
            elif isinstance(data.get("product"), dict):
                items = [data["product"]]
            else:
                return []
            return [_parse_product(p) for p in items if isinstance(p, dict)]
        # HTML: schema.org JSON-LD fallback (shared extraction, no I/O).
        return MockStoreAdapter().parse(page, schema)
