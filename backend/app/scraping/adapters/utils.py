"""Shared utilities for HTML-based adapters."""

from __future__ import annotations

import json
from typing import Any

from selectolax.parser import HTMLParser

from app.core.logging import get_logger
from app.scraping.types import RawFields

logger = get_logger(__name__)

_JSON_LD_SELECTOR = 'script[type="application/ld+json"]'


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return None


def _first_if_list(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def parse_json_ld(html: str) -> list[RawFields]:
    """Extract every JSON-LD Product block from HTML."""
    tree = HTMLParser(html)
    results: list[RawFields] = []

    for node in tree.css(_JSON_LD_SELECTOR):
        text = node.text(strip=True)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue

        candidates = parsed if isinstance(parsed, list) else [parsed]
        for candidate in candidates:
            if isinstance(candidate, dict):
                # Handle @graph
                if "@graph" in candidate and isinstance(candidate["@graph"], list):
                    for item in candidate["@graph"]:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            results.append(_extract_from_json_ld(item))
                elif candidate.get("@type") == "Product":
                    results.append(_extract_from_json_ld(candidate))

    return results


def _extract_from_json_ld(data: dict[str, Any]) -> RawFields:
    offers = _first_if_list(data.get("offers"))
    offers_dict = offers if isinstance(offers, dict) else {}

    rating = data.get("aggregateRating")
    rating_dict = rating if isinstance(rating, dict) else {}

    brand = data.get("brand")
    brand_name = brand.get("name") if isinstance(brand, dict) else brand

    image = _first_if_list(data.get("image"))

    return RawFields(
        product_name=_as_str(data.get("name")),
        brand=_as_str(brand_name),
        category=_as_str(data.get("category")),
        price=_as_str(offers_dict.get("price")),
        price_currency=_as_str(offers_dict.get("priceCurrency")),
        sku=_as_str(data.get("sku")),
        product_id=_as_str(data.get("productID")),
        stock_status_raw=_as_str(offers_dict.get("availability")),
        rating=_as_str(rating_dict.get("ratingValue")),
        review_count=_as_str(rating_dict.get("reviewCount")),
        description=_as_str(data.get("description")),
        image_url=_as_str(image),
    )
