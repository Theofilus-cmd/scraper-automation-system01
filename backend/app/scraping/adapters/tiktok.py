"""TikTok Shop US Adapter."""

from __future__ import annotations

import json
from typing import ClassVar
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from app.scraping import fetcher
from app.scraping.adapters.utils import parse_json_ld
from app.scraping.types import ExtractionSchema, FetchContext, RawFields, RawPage, SiteSniff


class TikTokShopUSAdapter:
    slug: ClassVar[str] = "tiktok_shop_us"
    requires_js: ClassVar[bool] = True

    def matches(self, url: str, sniff: SiteSniff) -> float:
        host = urlparse(url).hostname or ""
        if "tiktok.com" in host and "/shop/" in url:
            return 1.0
        return 0.0

    async def fetch(self, url: str, ctx: FetchContext | None = None) -> RawPage:
        return await fetcher.fetch(url)

    def parse(self, page: RawPage, schema: ExtractionSchema | None = None) -> list[RawFields]:
        results = parse_json_ld(page.html)
        if results:
            return results

        # TikTok often hides data in large JSON scripts
        tree = HTMLParser(page.html)
        for script in tree.css("script#RENDER_DATA"):
            try:
                json.loads(script.text())
                # This is highly site-specific and brittle, JSON-LD is preferred
                # But as a fallback, we'd dig into the RENDER_DATA structure
                pass
            except Exception:
                continue

        # Very basic fallback
        product_name = tree.css_first("h1")
        if product_name:
            return [
                RawFields(
                    product_name=product_name.text(strip=True),
                )
            ]

        return []
