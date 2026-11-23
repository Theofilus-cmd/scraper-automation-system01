"""Tests for ShopifyAdapter: URL matching, JSON-endpoint mapping, parsing
of /products.json + single-product JSON, JSON-LD fallback, and fetch()
going through the shared fetcher (httpx.MockTransport, no network)."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.scraping import fetcher
from app.scraping.adapters import registry
from app.scraping.adapters.shopify import ShopifyAdapter, _json_url
from app.scraping.normalize import normalize
from app.scraping.types import RawPage, SiteSniff

PRODUCT = {
    "id": 123,
    "title": "Blue Mug",
    "vendor": "Acme",
    "product_type": "Kitchen",
    "body_html": "<p>A <b>nice</b> mug</p>",
    "variants": [
        {
            "title": "Default Title",
            "price": "19.99",
            "compare_at_price": "24.99",
            "sku": "MUG-1",
            "available": True,
        },
    ],
    "image": {"src": "https://cdn.example.com/mug.png"},
}


def _page(text: str) -> RawPage:
    return RawPage(
        html=text,
        final_url="https://shop.example.com/x",
        status_code=200,
        fetched_at=datetime.now(UTC),
    )


def test_matches_shopify_url_shapes() -> None:
    a = ShopifyAdapter()
    s = SiteSniff()
    assert a.matches("https://shop.example.com/products/blue-mug", s) == 0.6
    assert a.matches("https://shop.example.com/collections/all/products/blue-mug", s) == 0.6
    assert a.matches("https://shop.example.com/products.json", s) == 0.6
    assert a.matches("https://shop.example.com/collections/mugs", s) == 0.5
    assert a.matches("https://shop.example.com/about", s) == 0.0


def test_json_url_mapping() -> None:
    assert _json_url("https://s.com/products/m?variant=1") == "https://s.com/products/m.json"
    assert _json_url("https://s.com/collections/all/products/m") == "https://s.com/products/m.json"
    assert _json_url("https://s.com/products.json") == "https://s.com/products.json?limit=250"
    assert (
        _json_url("https://s.com/collections/mugs")
        == "https://s.com/collections/mugs/products.json?limit=250"
    )


def test_registry_detects_shopify_but_not_unknown() -> None:
    adapter = registry.detect("https://shop.example.com/products/blue-mug")
    assert adapter is not None and adapter.slug == "shopify"
    assert registry.detect("https://shop.example.com/about") is None


def test_parse_products_json_and_normalize() -> None:
    out = ShopifyAdapter().parse(_page(json.dumps({"products": [PRODUCT, PRODUCT]})))
    assert len(out) == 2
    assert out[0]["product_name"] == "Blue Mug"
    assert out[0]["price"] == "19.99"
    assert out[0]["original_price"] == "24.99"
    assert out[0]["description"] == "A nice mug"
    assert out[0]["variant"] is None
    rec = normalize(out[0], "https://shop.example.com/products/blue-mug")
    assert rec.stock_status == "in_stock"
    assert str(rec.price) == "19.99"


def test_parse_single_product_out_of_stock() -> None:
    p = {**PRODUCT, "variants": [{"title": "Large", "price": "5", "available": False}]}
    out = ShopifyAdapter().parse(_page(json.dumps({"product": p})))
    assert out[0]["stock_status_raw"] == "OutOfStock"
    assert out[0]["variant"] == "Large"


@pytest.mark.parametrize("text", ["{bad", "[]", '{"other": 1}'])
def test_parse_bad_json_returns_empty(text: str) -> None:
    assert ShopifyAdapter().parse(_page(text)) == []


def test_parse_json_ld_fallback() -> None:
    ld = json.dumps(
        {"@type": "Product", "name": "LD Mug", "offers": {"price": "3", "priceCurrency": "USD"}}
    )
    html = f'<html><script type="application/ld+json">{ld}</script></html>'
    out = ShopifyAdapter().parse(_page(html))
    assert out[0]["product_name"] == "LD Mug"


async def test_fetch_uses_json_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = fetcher.fetch

    async def fake(url: str, **_: object) -> RawPage:
        calls.append(url)

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=json.dumps({"product": PRODUCT}))

        monkeypatch.setattr(fetcher, "_check_url_allowed", lambda u: None)
        return await real(url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(fetcher, "fetch", fake)
    page = await ShopifyAdapter().fetch("https://shop.example.com/products/blue-mug")
    assert calls == [
        "https://shop.example.com/products/blue-mug.json",
        "https://shop.example.com/meta.json",
    ]
    assert ShopifyAdapter().parse(page)[0]["product_name"] == "Blue Mug"


def test_parse_defaults_currency_to_usd() -> None:
    out = ShopifyAdapter().parse(_page(json.dumps({"products": [PRODUCT]})))
    assert out[0]["price_currency"] == "USD"
    assert normalize(out[0], "https://shop.example.com/products.json").currency == "USD"


async def test_fetch_reads_currency_from_meta_json(monkeypatch: pytest.MonkeyPatch) -> None:
    real = fetcher.fetch

    async def fake(url: str, **_: object) -> RawPage:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/meta.json":
                return httpx.Response(200, text=json.dumps({"currency": "EUR"}))
            return httpx.Response(200, text=json.dumps({"products": [PRODUCT]}))

        monkeypatch.setattr(fetcher, "_check_url_allowed", lambda u: None)
        return await real(url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(fetcher, "fetch", fake)
    page = await ShopifyAdapter().fetch("https://shop.example.com/products.json")
    assert ShopifyAdapter().parse(page)[0]["price_currency"] == "EUR"


async def test_fetch_falls_back_to_html_when_json_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = fetcher.fetch

    async def fake(url: str, **_: object) -> RawPage:
        calls.append(url)

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith(".json"):
                return httpx.Response(404, text="nope")
            return httpx.Response(200, text="<html></html>")

        monkeypatch.setattr(fetcher, "_check_url_allowed", lambda u: None)
        return await real(url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(fetcher, "fetch", fake)
    await ShopifyAdapter().fetch("https://shop.example.com/products/blue-mug")
    assert calls == [
        "https://shop.example.com/products/blue-mug.json",
        "https://shop.example.com/products/blue-mug",
    ]
