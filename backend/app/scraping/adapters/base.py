"""SourceAdapter Protocol (doc 08 §1) -- structural typing only, no shared
base class. `registry.py` holds the concrete adapter list and detect();
this module holds only the shape every adapter must satisfy.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from app.scraping.types import ExtractionSchema, FetchContext, RawFields, RawPage, SiteSniff


@runtime_checkable
class SourceAdapter(Protocol):
    """doc 08 §1's adapter contract.

    `fetch()` must delegate to `app.scraping.fetcher.fetch()` -- never a raw
    httpx/requests/Playwright call (see that module's docstring for why).
    `parse()` must extract raw, uninterpreted strings only; all
    interpretation (currency inference, stock-status vocabulary mapping,
    decimal parsing, ...) belongs in `app.scraping.normalize.normalize()`,
    never here -- keeping adapter-specific extraction and adapter-agnostic
    interpretation strictly separated.
    """

    slug: ClassVar[str]
    requires_js: ClassVar[bool]

    def matches(self, url: str, sniff: SiteSniff) -> float:
        """Confidence in [0.0, 1.0] that this adapter can handle `url`.

        Pure -- no I/O, no network. `sniff` is a cheap pre-fetch signal
        (doc 08 §2); Phase 1's only adapter ignores it (hostname check
        only), but the signature stays stable for later, content-sniffing
        adapters.
        """
        ...

    async def fetch(self, url: str, ctx: FetchContext) -> RawPage:
        """Fetch `url`. Must delegate to the shared SSRF-hardened fetcher."""
        ...

    def parse(self, page: RawPage, schema: ExtractionSchema) -> list[RawFields]:
        """Extract one `RawFields` dict per product found on `page`.

        Raw, uninterpreted strings only -- see class docstring.
        """
        ...
