"""Alibaba.com Adapter."""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from app.scraping import fetcher
from app.scraping.adapters.utils import parse_json_ld
from app.scraping.types import ExtractionSchema, FetchContext, RawFields, RawPage, SiteSniff


class AlibabaAdapter:
    slug: ClassVar[str] = "alibaba"
    requires_js: ClassVar[bool] = False

    def matches(self, url: str, sniff: SiteSniff) -> float:
        host = urlparse(url).hostname or ""
        if host == "alibaba.com" or host.endswith(".alibaba.com"):
            return 1.0
        return 0.0

    async def fetch(self, url: str, ctx: FetchContext | None = None) -> RawPage:
        return await fetcher.fetch(url)

    def parse(self, page: RawPage, schema: ExtractionSchema | None = None) -> list[RawFields]:
        results = parse_json_ld(page.html)
        if results:
            return results

        tree = HTMLParser(page.html)
        product_name = tree.css_first(".module-pdp-title")
        price = tree.css_first(".price-item")
        
        if product_name:
            return [
                RawFields(
                    product_name=product_name.text(strip=True),
                    price=price.text(strip=True) if price else None,
                )
            ]

        return []
