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

from typing import ClassVar
from urllib.parse import urlparse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.scraping import fetcher
from app.scraping.adapters.utils import parse_json_ld
from app.scraping.types import ExtractionSchema, FetchContext, RawFields, RawPage, SiteSniff

logger = get_logger(__name__)


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
        return parse_json_ld(page.html)
