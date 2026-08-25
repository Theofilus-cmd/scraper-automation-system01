"""Unit tests for app/domain/validation.py -- pure, no I/O.

doc 08 §4's outcome table: required fields are product_name, price,
currency, stock_status, product_url (+ system-set scraped_at). A failure
in any of those flips is_valid to False; optional-field parse_warnings
populate `errors` alongside them but never affect is_valid (the "partial
success" row).
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.validation import validate
from app.scraping.types import NormalizedRecord


def _valid_record() -> NormalizedRecord:
    return NormalizedRecord(
        product_url="https://example.com/products/widget",
        scraped_at=datetime.now(UTC),
        stock_status="in_stock",
        product_name="Widget",
        price=Decimal("9.99"),
        currency="USD",
    )


def test_fully_valid_record_has_no_errors() -> None:
    result = validate(_valid_record())
    assert result.is_valid is True
    assert result.errors == {}


def test_missing_product_name_fails_validation() -> None:
    result = validate(replace(_valid_record(), product_name=None))
    assert result.is_valid is False
    assert "product_name" in result.errors


def test_empty_string_product_name_fails_validation() -> None:
    result = validate(replace(_valid_record(), product_name=""))
    assert result.is_valid is False
    assert "product_name" in result.errors


def test_missing_price_fails_validation() -> None:
    result = validate(replace(_valid_record(), price=None))
    assert result.is_valid is False
    assert "price" in result.errors


def test_zero_price_fails_validation() -> None:
    result = validate(replace(_valid_record(), price=Decimal("0")))
    assert result.is_valid is False
    assert "price" in result.errors


def test_negative_price_fails_validation() -> None:
    result = validate(replace(_valid_record(), price=Decimal("-5")))
    assert result.is_valid is False
    assert "price" in result.errors


def test_missing_currency_fails_validation() -> None:
    result = validate(replace(_valid_record(), currency=None))
    assert result.is_valid is False
    assert "currency" in result.errors


def test_parse_warnings_populate_errors_without_failing_validation() -> None:
    record = replace(
        _valid_record(),
        parse_warnings={"review_count": "could not parse review_count: 'lots'"},
    )
    result = validate(record)

    assert result.is_valid is True
    assert result.errors == {"review_count": "could not parse review_count: 'lots'"}


def test_multiple_required_failures_all_reported_together() -> None:
    result = validate(replace(_valid_record(), product_name=None, price=None))
    assert result.is_valid is False
    assert {"product_name", "price"} <= result.errors.keys()
