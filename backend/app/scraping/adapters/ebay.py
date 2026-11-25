"""eBay US Adapter."""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from app.scraping import fetcher
from app.scraping.adapters.utils import parse_json_ld
from app.scraping.types import ExtractionSchema, FetchContext, RawFields, RawPage, SiteSniff


class EbayUSAdapter:
    slug: ClassVar[str] = "ebay_us"
    requires_js: ClassVar[bool] = False

    def matches(self, url: str, sniff: SiteSniff) -> float:
        host = urlparse(url).hostname or ""
        if host == "ebay.com" or host.endswith(".ebay.com"):
            return 1.0
        return 0.0

    async def fetch(self, url: str, ctx: FetchContext | None = None) -> RawPage:
        return await fetcher.fetch(url)

    def parse(self, page: RawPage, schema: ExtractionSchema | None = None) -> list[RawFields]:
        results = parse_json_ld(page.html)
        if results:
            return results

        tree = HTMLParser(page.html)
        product_name = tree.css_first("h1.x-item-title__mainTitle")
        price = tree.css_first(".x-price-primary")
        image = tree.css_first(".ux-image-magnify__image--read-only")

        if product_name:
            return [
                RawFields(
                    product_name=product_name.text(strip=True),
                    price=price.text(strip=True) if price else None,
                    image_url=image.attributes.get("src") if image else None,
                    price_currency="USD",
                )
            ]

        return []
