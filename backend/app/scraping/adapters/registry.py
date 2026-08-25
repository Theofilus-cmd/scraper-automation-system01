"""Adapter registry and detection (doc 08 §2).

Phase 1 ships exactly one adapter and no generic fallback, so `detect()`
returns `SourceAdapter | None` rather than doc 08 §2's literal
always-falls-back-to-generic behavior -- a necessary, explicitly-flagged
deviation (doc 17's Scope Decision 1) until a generic adapter exists. A
`None` result is a real, expected outcome for any non-mock-store URL, not
an error condition.
"""

from __future__ import annotations

from app.scraping.adapters.base import SourceAdapter
from app.scraping.adapters.mock_store import MockStoreAdapter
from app.scraping.types import SiteSniff

ADAPTERS: list[SourceAdapter] = [MockStoreAdapter()]

# doc 08 §2's confidence floor below which a match is not trusted. With no
# generic adapter yet to fall back to, anything at or below this floor
# returns None (Scope Decision 1) instead of a low-confidence guess.
MATCH_CONFIDENCE_FLOOR = 0.3


def detect(url: str) -> SourceAdapter | None:
    """Return the best-matching adapter for `url`, or `None` if nothing
    scores above `MATCH_CONFIDENCE_FLOOR`.

    Pure -- no I/O. `SiteSniff` is constructed empty; no adapter in Phase 1
    needs a real pre-fetch sniff yet (see `SiteSniff`'s docstring).
    """
    sniff = SiteSniff()
    scored = [(adapter, adapter.matches(url, sniff)) for adapter in ADAPTERS]
    if not scored:
        return None
    best_adapter, best_score = max(scored, key=lambda pair: pair[1])
    if best_score <= MATCH_CONFIDENCE_FLOOR:
        return None
    return best_adapter
