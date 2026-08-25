"""Adapter-agnostic normalize() (doc 08 §1/§3) -- pure, no I/O.

Every adapter's parse() output goes through this same function; platform-
specific knowledge must never leak in here (doc 08 §1: "nothing downstream
of parse() knows or cares which adapter produced a record").
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse, urlunparse

from app.scraping.types import NormalizedRecord, RawFields

_WHITESPACE_RE = re.compile(r"\s+")

# Tracking query params stripped during canonicalization (doc 08 §3
# "canonicalized (strip tracking params)", doc 05 §4 "normalized_url").
# Not exhaustive -- the common cross-platform set, extend as real adapters
# surface more.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "ref", "ref_", "igshid",
}

_SYMBOL_TO_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}

# Wide source vocabulary -> the 4-value enum (doc 08 §3). Keys are matched
# against a lowercased, whitespace-collapsed raw string; schema.org URI
# values (e.g. "https://schema.org/InStock") are stripped to their bare
# suffix before this lookup, so the same table serves both JSON-LD adapters
# and free-text ones.
_STOCK_STATUS_MAP = {
    "instock": "in_stock",
    "in stock": "in_stock",
    "add to cart": "in_stock",
    "limitedavailability": "in_stock",
    "onlineonly": "in_stock",
    "instoreonly": "in_stock",
    "outofstock": "out_of_stock",
    "out of stock": "out_of_stock",
    "sold out": "out_of_stock",
    "soldout": "out_of_stock",
    "discontinued": "out_of_stock",
    "preorder": "preorder",
    "pre-order": "preorder",
    "pre order": "preorder",
    "presale": "preorder",
    "coming soon": "preorder",
    "backorder": "preorder",
    "back order": "preorder",
}


def normalize_url(url: str) -> str:
    """Canonicalize a URL for dedup/identity purposes (doc 05 §4
    `normalized_url`, doc 08 §3 `product_url`): lowercase scheme+host, drop
    the fragment, strip known tracking params, drop a trailing slash from
    a non-root path.
    """
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"

    kept_params = [
        (k, v)
        for k, v in _parse_qsl_sorted(parsed.query)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = "&".join(f"{k}={v}" for k, v in kept_params)

    return urlunparse((scheme, netloc, path, "", query, ""))


def _parse_qsl_sorted(query: str) -> list[tuple[str, str]]:
    if not query:
        return []
    pairs = [tuple(part.split("=", 1)) if "=" in part else (part, "") for part in query.split("&")]
    return sorted((str(k), str(v)) for k, v in pairs)


def _clean_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = _WHITESPACE_RE.sub(" ", html.unescape(raw)).strip()
    return cleaned or None


def _title_case_if_all_caps(text: str | None) -> str | None:
    if text is None:
        return None
    return text.title() if text.isupper() else text


def _parse_decimal(raw: str | None) -> Decimal | None:
    """Strip currency symbols/whitespace, resolve `,` vs `.` as the decimal
    separator (doc 08 §3 "locale-aware decimal parsing"). Heuristic: if
    both separators appear, the rightmost one is the decimal separator and
    the other is a thousands separator; if only `,` appears with exactly
    two trailing digits, treat it as decimal (European style); otherwise
    treat it as a thousands separator.
    """
    if raw is None:
        return None
    stripped = re.sub(r"[^\d,.\-]", "", raw.strip())
    if not stripped:
        return None

    last_comma = stripped.rfind(",")
    last_period = stripped.rfind(".")
    if last_comma != -1 and last_period != -1:
        if last_comma > last_period:
            stripped = stripped.replace(".", "").replace(",", ".")
        else:
            stripped = stripped.replace(",", "")
    elif last_comma != -1:
        after = stripped[last_comma + 1 :]
        stripped = (
            stripped.replace(",", ".") if len(after) == 2 else stripped.replace(",", "")
        )
    # else: only '.' or neither -- already in a Decimal-parseable shape.

    try:
        value = Decimal(stripped)
    except InvalidOperation:
        return None
    return value


def _parse_currency(price_currency: str | None, price_raw: str | None) -> str | None:
    if price_currency:
        code = price_currency.strip().upper()
        if re.fullmatch(r"[A-Z]{3}", code):
            return code
    if price_raw:
        for symbol, code in _SYMBOL_TO_CURRENCY.items():
            if symbol in price_raw:
                return code
    return None


def _map_stock_status(raw: str | None) -> str:
    if not raw:
        return "unknown"
    bare = raw.strip()
    for prefix in ("https://schema.org/", "http://schema.org/", "schema:"):
        if bare.lower().startswith(prefix):
            bare = bare[len(prefix) :]
            break
    return _STOCK_STATUS_MAP.get(bare.strip().lower(), "unknown")


def _parse_rating(raw: str | None) -> Decimal | None:
    value = _parse_decimal(raw)
    if value is None:
        return None
    if value < 0 or value > 5:
        return None  # doc 08 §3: out-of-range values discarded, not clamped
    return value


def _parse_review_count(raw: str | None) -> int | None:
    if raw is None:
        return None
    digits = raw.strip()
    if not re.fullmatch(r"\d+", digits):
        return None  # doc 08 §3: discarded if non-numeric, not guessed
    return int(digits)


def _resolve_image_url(raw: str | None, source_url: str) -> str | None:
    if not raw:
        return None
    resolved = urljoin(source_url, raw.strip())
    if urlparse(resolved).scheme not in ("http", "https"):
        return None
    return resolved


_DESCRIPTION_MAX_LEN = 5000


def normalize(raw: RawFields, source_url: str) -> NormalizedRecord:
    """doc 08 §1/§3: turn one adapter's raw extraction into the canonical
    schema. Never raises for a merely-unparseable field -- that's what
    `parse_warnings` and `validate()` are for; only truly malformed input
    (not this function's job to anticipate) should ever raise.
    """
    warnings: dict[str, str] = {}

    price = _parse_decimal(raw.get("price"))
    if raw.get("price") and price is None:
        warnings["price"] = f"could not parse price: {raw.get('price')!r}"

    original_price = _parse_decimal(raw.get("original_price"))
    if raw.get("original_price") and original_price is None:
        warnings["original_price"] = (
            f"could not parse original_price: {raw.get('original_price')!r}"
        )

    discount = _parse_decimal(raw.get("discount"))
    if discount is None and original_price is not None and price is not None and original_price > 0:
        discount = ((original_price - price) / original_price * 100).quantize(Decimal("0.01"))

    rating_raw = raw.get("rating")
    rating = _parse_rating(rating_raw)
    if rating_raw and rating is None:
        warnings["rating"] = f"could not parse or out-of-range rating: {rating_raw!r}"

    review_count_raw = raw.get("review_count")
    review_count = _parse_review_count(review_count_raw)
    if review_count_raw and review_count is None:
        warnings["review_count"] = f"could not parse review_count: {review_count_raw!r}"

    description = _clean_text(raw.get("description"))
    if description and len(description) > _DESCRIPTION_MAX_LEN:
        description = description[:_DESCRIPTION_MAX_LEN]

    return NormalizedRecord(
        product_url=normalize_url(source_url),
        scraped_at=datetime.now(UTC),
        stock_status=_map_stock_status(raw.get("stock_status_raw")),
        product_name=_clean_text(raw.get("product_name")),
        brand=_title_case_if_all_caps(_clean_text(raw.get("brand"))),
        category=_clean_text(raw.get("category")),
        price=price,
        currency=_parse_currency(raw.get("price_currency"), raw.get("price")),
        original_price=original_price,
        discount=discount,
        sku=_clean_text(raw.get("sku")),
        product_id=_clean_text(raw.get("product_id")),
        variant=_clean_text(raw.get("variant")),
        rating=rating,
        review_count=review_count,
        description=description,
        image_url=_resolve_image_url(raw.get("image_url"), source_url),
        parse_warnings=warnings,
    )
