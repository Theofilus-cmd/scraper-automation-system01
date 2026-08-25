"""validate() -- doc 08 §4's outcome/task-status mapping, pure, no I/O.

Required fields: product_name, price, currency, stock_status, product_url
(plus system-set scraped_at, which NormalizedRecord guarantees non-None by
construction and so can never fail here). A record failing any required
field is a *failure* -- the repository layer (app/db/models/repository.py)
is what actually enforces "don't write/update on failure, leave the prior
snapshot standing"; this function only classifies, it never touches the DB.
"""

from __future__ import annotations

from app.scraping.types import NormalizedRecord, ValidationResult


def validate(record: NormalizedRecord) -> ValidationResult:
    errors: dict[str, str] = {}

    if not record.product_name:
        errors["product_name"] = "missing required field: product_name"

    if record.price is None:
        errors["price"] = "missing required field: price"
    elif record.price <= 0:
        errors["price"] = "price must be a positive number"

    if not record.currency:
        errors["currency"] = "missing required field: currency"

    if not record.stock_status:
        # Structurally unreachable today -- normalize() always sets this to
        # one of the 4 enum values, never None/empty (doc 08 §3). Checked
        # anyway so this function stays correct if that guarantee ever
        # changes, rather than silently trusting the caller.
        errors["stock_status"] = "missing required field: stock_status"

    if not record.product_url:
        # Also structurally unreachable today, same reasoning as above.
        errors["product_url"] = "missing required field: product_url"

    required_fields = ("product_name", "price", "currency", "stock_status", "product_url")
    is_valid = not any(field in errors for field in required_fields)

    # Optional-field parse failures (doc 08 §4's "partial success" row):
    # recorded alongside required-field errors in the same `errors` dict so
    # callers get one place to look, but they never flip `is_valid` to
    # False -- only entries under `required_fields` above can do that.
    errors.update(record.parse_warnings)

    return ValidationResult(is_valid=is_valid, errors=errors)
