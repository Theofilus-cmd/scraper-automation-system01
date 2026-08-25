"""Golden-fixture tests for MockStoreAdapter (doc 12 §2's pattern:
tests/fixtures/adapters/<adapter>/<case>.{html,expected.json}).

parse() only ever runs against an already-fetched RawPage -- these tests
build one directly from fixture HTML and never touch the network.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.scraping.adapters.mock_store import MockStoreAdapter
from app.scraping.types import RawPage, SiteSniff

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "adapters" / "mock_store"
_CASES = [
    "widget-in-stock",
    "widget-out-of-stock",
    "widget-missing-price",
    "widget-invalid-review-count",
]


def _page_from_fixture(case: str) -> RawPage:
    html = (_FIXTURES_DIR / f"{case}.html").read_text()
    return RawPage(
        html=html,
        final_url=f"https://example.com/products/{case}",
        status_code=200,
        fetched_at=datetime.now(UTC),
    )


def _expected(case: str) -> list[dict[str, object]]:
    text = (_FIXTURES_DIR / f"{case}.expected.json").read_text()
    data: list[dict[str, object]] = json.loads(text)
    return data


@pytest.mark.parametrize("case", _CASES)
def test_parse_matches_golden_fixture(case: str) -> None:
    adapter = MockStoreAdapter()

    result = adapter.parse(_page_from_fixture(case), schema=None)

    assert result == _expected(case)


def test_parse_ignores_non_product_json_ld_blocks() -> None:
    # widget-in-stock.html deliberately includes a second JSON-LD block
    # (@type: BreadcrumbList) alongside the real Product block.
    adapter = MockStoreAdapter()

    result = adapter.parse(_page_from_fixture("widget-in-stock"), schema=None)

    assert len(result) == 1


def test_parse_returns_empty_list_when_no_product_json_ld_present() -> None:
    adapter = MockStoreAdapter()
    page = RawPage(
        html="<html><body><h1>No structured data here</h1></body></html>",
        final_url="https://example.com/empty",
        status_code=200,
        fetched_at=datetime.now(UTC),
    )

    assert adapter.parse(page, schema=None) == []


def test_parse_skips_malformed_json_ld_without_raising() -> None:
    adapter = MockStoreAdapter()
    page = RawPage(
        html=(
            '<html><head><script type="application/ld+json">{not valid json</script>'
            "</head><body></body></html>"
        ),
        final_url="https://example.com/broken",
        status_code=200,
        fetched_at=datetime.now(UTC),
    )

    assert adapter.parse(page, schema=None) == []


def test_matches_scores_mock_store_hostname_and_rejects_others() -> None:
    adapter = MockStoreAdapter()

    assert adapter.matches("http://mock-store:4000/products/widget-in-stock", SiteSniff()) == 1.0
    assert adapter.matches("https://real-marketplace.example.com/p/1", SiteSniff()) == 0.0
