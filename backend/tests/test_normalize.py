"""Unit tests for app/scraping/normalize.py -- pure, no I/O."""

from decimal import Decimal

from app.scraping.normalize import normalize, normalize_url
from app.scraping.types import RawFields

_SOURCE_URL = "https://example.com/products/widget"


def test_normalize_url_lowercases_scheme_and_host_and_strips_fragment() -> None:
    assert normalize_url("HTTPS://Example.COM/Path/#section") == "https://example.com/Path"


def test_normalize_url_strips_tracking_params_but_keeps_others() -> None:
    url = "https://example.com/p?utm_source=ads&id=42&fbclid=abc"
    assert normalize_url(url) == "https://example.com/p?id=42"


def test_normalize_url_drops_trailing_slash_on_non_root_path() -> None:
    assert normalize_url("https://example.com/products/widget/") == (
        "https://example.com/products/widget"
    )


def test_normalize_url_keeps_root_slash() -> None:
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_normalize_minimal_record_sets_system_fields() -> None:
    raw: RawFields = {"product_name": "Widget", "price": "9.99", "stock_status_raw": "InStock"}
    record = normalize(raw, _SOURCE_URL)

    assert record.product_url == _SOURCE_URL
    assert record.scraped_at is not None
    assert record.stock_status == "in_stock"
    assert record.product_name == "Widget"
    assert record.price == Decimal("9.99")


def test_normalize_price_comma_decimal_heuristic() -> None:
    record = normalize({"price": "39,99"}, _SOURCE_URL)
    assert record.price == Decimal("39.99")


def test_normalize_price_european_thousands_and_decimal() -> None:
    # '.' as thousands separator, ',' as decimal separator.
    record = normalize({"price": "1.234,56"}, _SOURCE_URL)
    assert record.price == Decimal("1234.56")


def test_normalize_price_us_thousands_separator() -> None:
    record = normalize({"price": "1,234.56"}, _SOURCE_URL)
    assert record.price == Decimal("1234.56")


def test_normalize_price_missing_produces_no_warning() -> None:
    record = normalize({}, _SOURCE_URL)
    assert record.price is None
    assert "price" not in record.parse_warnings


def test_normalize_price_present_but_unparseable_produces_warning() -> None:
    record = normalize({"price": "call for pricing"}, _SOURCE_URL)
    assert record.price is None
    assert "price" in record.parse_warnings


def test_normalize_currency_explicit_code_wins_over_symbol() -> None:
    record = normalize({"price": "$9.99", "price_currency": "EUR"}, _SOURCE_URL)
    assert record.currency == "EUR"


def test_normalize_currency_inferred_from_symbol_when_no_explicit_code() -> None:
    record = normalize({"price": "£9.99"}, _SOURCE_URL)
    assert record.currency == "GBP"


def test_normalize_stock_status_strips_schema_org_prefix() -> None:
    record = normalize({"stock_status_raw": "https://schema.org/OutOfStock"}, _SOURCE_URL)
    assert record.stock_status == "out_of_stock"


def test_normalize_stock_status_defaults_to_unknown_never_guesses_in_stock() -> None:
    record = normalize({"stock_status_raw": "Ask an associate"}, _SOURCE_URL)
    assert record.stock_status == "unknown"


def test_normalize_stock_status_missing_defaults_to_unknown() -> None:
    record = normalize({}, _SOURCE_URL)
    assert record.stock_status == "unknown"


def test_normalize_discount_derived_when_not_source_reported() -> None:
    record = normalize({"price": "80", "original_price": "100"}, _SOURCE_URL)
    assert record.discount == Decimal("20.00")


def test_normalize_discount_uses_source_value_when_present() -> None:
    record = normalize(
        {"price": "80", "original_price": "100", "discount": "15"}, _SOURCE_URL
    )
    assert record.discount == Decimal("15")


def test_normalize_rating_out_of_range_is_discarded_not_clamped() -> None:
    record = normalize({"rating": "7.0"}, _SOURCE_URL)
    assert record.rating is None
    assert "rating" in record.parse_warnings


def test_normalize_rating_within_range_is_kept() -> None:
    record = normalize({"rating": "4.5"}, _SOURCE_URL)
    assert record.rating == Decimal("4.5")
    assert "rating" not in record.parse_warnings


def test_normalize_review_count_non_numeric_is_discarded_with_warning() -> None:
    record = normalize({"review_count": "lots"}, _SOURCE_URL)
    assert record.review_count is None
    assert "review_count" in record.parse_warnings


def test_normalize_review_count_numeric_is_kept() -> None:
    record = normalize({"review_count": "128"}, _SOURCE_URL)
    assert record.review_count == 128


def test_normalize_image_url_resolved_against_source_url() -> None:
    record = normalize(
        {"image_url": "/images/widget.jpg"}, "https://example.com/products/widget"
    )
    assert record.image_url == "https://example.com/images/widget.jpg"


def test_normalize_image_url_rejects_non_http_scheme() -> None:
    record = normalize({"image_url": "javascript:alert(1)"}, _SOURCE_URL)
    assert record.image_url is None


def test_normalize_description_truncated_at_5000_chars() -> None:
    record = normalize({"description": "x" * 6000}, _SOURCE_URL)
    assert record.description is not None
    assert len(record.description) == 5000


def test_normalize_brand_titlecased_only_if_all_caps() -> None:
    assert normalize({"brand": "ACME"}, _SOURCE_URL).brand == "Acme"
    assert normalize({"brand": "Acme Co"}, _SOURCE_URL).brand == "Acme Co"


def test_normalize_text_fields_collapse_whitespace_and_decode_entities() -> None:
    record = normalize({"product_name": "  Widget &amp;  Gadget  "}, _SOURCE_URL)
    assert record.product_name == "Widget & Gadget"
