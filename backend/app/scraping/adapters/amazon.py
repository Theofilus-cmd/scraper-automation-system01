"""Amazon US Adapter."""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from app.scraping import fetcher
from app.scraping.adapters.utils import parse_json_ld
from app.scraping.types import ExtractionSchema, FetchContext, RawFields, RawPage, SiteSniff


class AmazonUSAdapter:
    slug: ClassVar[str] = "amazon_us"
    requires_js: ClassVar[bool] = True  # Amazon often requires JS for full content

    def matches(self, url: str, sniff: SiteSniff) -> float:
        host = urlparse(url).hostname or ""
        if host == "amazon.com" or host.endswith(".amazon.com"):
            return 1.0
        return 0.0

    async def fetch(self, url: str, ctx: FetchContext | None = None) -> RawPage:
        return await fetcher.fetch(url)

    def parse(self, page: RawPage, schema: ExtractionSchema | None = None) -> list[RawFields]:
        # Try JSON-LD first
        results = parse_json_ld(page.html)
        if results:
            return results

        # Fallback to selectors
        tree = HTMLParser(page.html)
        product_name = tree.css_first("#productTitle")
        price = tree.css_first(".a-price .a-offscreen")
        brand = tree.css_first("#bylineInfo")
        image = tree.css_first("#landingImage")

        if product_name:
            return [
                RawFields(
                    product_name=product_name.text(strip=True),
                    price=price.text(strip=True) if price else None,
                    brand=brand.text(strip=True) if brand else None,
                    image_url=image.attributes.get("src") if image else None,
                    price_currency="USD",  # Amazon US is USD
                )
            ]

        return []
