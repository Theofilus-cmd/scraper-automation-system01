"""Tests for multi-marketplace adapters (Amazon, eBay, Etsy, TikTok, Alibaba)."""

import json
from datetime import UTC, datetime

import pytest

from app.scraping.adapters import registry
from app.scraping.adapters.alibaba import AlibabaAdapter
from app.scraping.adapters.amazon import AmazonUSAdapter
from app.scraping.adapters.ebay import EbayUSAdapter
from app.scraping.adapters.etsy import EtsyAdapter
from app.scraping.adapters.tiktok import TikTokShopUSAdapter
from app.scraping.types import RawPage


def _page(text: str, url: str = "https://example.com") -> RawPage:
    return RawPage(
        html=text,
        final_url=url,
        status_code=200,
        fetched_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://www.amazon.com/dp/B000123456", "amazon_us"),
        ("https://www.ebay.com/itm/123456", "ebay_us"),
        ("https://www.etsy.com/listing/123456", "etsy"),
        ("https://www.tiktok.com/@user/shop/123456", "tiktok_shop_us"),
        ("https://www.alibaba.com/product-detail/xyz.html", "alibaba"),
    ],
)
def test_registry_detects_marketplaces(url: str, slug: str) -> None:
    adapter = registry.detect(url)
    assert adapter is not None
    assert adapter.slug == slug


def test_amazon_us_parse() -> None:
    html = (
        "<html>"
        '<h1 id="productTitle">Amazon Item</h1>'
        '<span class="a-price"><span class="a-offscreen">$99.99</span></span>'
        "</html>"
    )
    out = AmazonUSAdapter().parse(_page(html))
    assert out[0]["product_name"] == "Amazon Item"
    assert out[0]["price"] == "$99.99"
    assert out[0]["price_currency"] == "USD"


def test_ebay_us_parse() -> None:
    html = (
        "<html>"
        '<h1 class="x-item-title__mainTitle">eBay Item</h1>'
        '<span class="x-price-primary">US $45.00</span>'
        "</html>"
    )
    out = EbayUSAdapter().parse(_page(html))
    assert out[0]["product_name"] == "eBay Item"
    assert out[0]["price"] == "US $45.00"
    assert out[0]["price_currency"] == "USD"


def test_etsy_parse() -> None:
    ld = json.dumps(
        {
            "@type": "Product",
            "name": "Etsy Item",
            "offers": {"price": "25.00", "priceCurrency": "USD"},
        }
    )
    html = f'<html><script type="application/ld+json">{ld}</script></html>'
    out = EtsyAdapter().parse(_page(html))
    assert out[0]["product_name"] == "Etsy Item"
    assert out[0]["price"] == "25.00"


def test_tiktok_shop_us_parse() -> None:
    html = "<html><h1>TikTok Item</h1></html>"
    out = TikTokShopUSAdapter().parse(_page(html, "https://www.tiktok.com/@user/shop/1"))
    assert out[0]["product_name"] == "TikTok Item"


def test_alibaba_parse() -> None:
    html = (
        "<html>"
        '<h1 class="module-pdp-title">Alibaba Item</h1>'
        '<span class="price-item">$10.00</span>'
        "</html>"
    )
    out = AlibabaAdapter().parse(_page(html))
    assert out[0]["product_name"] == "Alibaba Item"
    assert out[0]["price"] == "$10.00"


def test_json_ld_shared_utility() -> None:
    # Test @graph support (common on WordPress/WooCommerce, sometimes others)
    ld = json.dumps({
        "@graph": [
            {"@type": "Product", "name": "Graph Item", "offers": {"price": "10"}}
        ]
    })
    html = f'<html><script type="application/ld+json">{ld}</script></html>'
    # Any adapter using parse_json_ld should pick this up
    out = AmazonUSAdapter().parse(_page(html))
    assert out[0]["product_name"] == "Graph Item"
