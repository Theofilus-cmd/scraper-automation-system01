"""Pure unit tests for price gap / position / MAP logic and request validation."""

from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from app.api.v1.internal_products import (
    CreateInternalProductRequest,
    PatchInternalProductRequest,
)
from app.api.v1.product_matches import CreateMatchRequest
from app.domain.price_intelligence import Competitor, compute_intelligence, price_position


def _comp(i: str, price: str | None, cur: str | None = "USD", valid: bool = True) -> Competitor:
    return Competitor(i, D(price) if price is not None else None, cur, valid)


@pytest.mark.parametrize(
    ("own", "prices", "expected"),
    [
        ("5", [], None),
        ("5", ["6", "8"], "lowest"),
        ("6", ["6", "8"], "lowest"),
        ("9", ["6", "8"], "highest"),
        ("8", ["6", "8"], "highest"),
        ("7", ["6", "8"], "average"),
        ("5", ["5"], "average"),
        ("4", ["5"], "lowest"),
        ("6", ["5"], "highest"),
    ],
)
def test_price_position(own: str, prices: list[str], expected: str | None) -> None:
    assert price_position(D(own), [D(p) for p in prices]) == expected


def test_gap_and_stats() -> None:
    r = compute_intelligence(
        own_price=D("110.00"),
        map_price=None,
        currency="USD",
        competitors=[_comp("a", "100.00"), _comp("b", "125.00")],
    )
    a, b = r.comparisons
    assert (a.price_gap, a.price_gap_pct) == (D("10.00"), D("10.00"))
    assert (b.price_gap, b.price_gap_pct) == (D("-15.00"), D("-12.00"))
    assert (r.min_price, r.max_price) == (D("100.00"), D("125.00"))
    assert r.avg_price == D("112.50")
    assert r.median_price == D("112.50")
    assert r.price_position == "average"
    assert r.map_violation_count == 0 and not r.own_price_below_map


def test_excludes_invalid_missing_and_foreign_currency() -> None:
    r = compute_intelligence(
        own_price=D("10"),
        map_price=None,
        currency="USD",
        competitors=[
            _comp("none", None),
            _comp("invalid", "5", valid=False),
            _comp("eur", "5", "EUR"),
            _comp("nocur", "5", None),
            _comp("ok", "5"),
        ],
    )
    assert [c.match_id for c in r.comparisons] == ["ok"]
    assert {e.match_id: e.reason for e in r.excluded} == {
        "none": "no_valid_observation",
        "invalid": "no_valid_observation",
        "eur": "currency_mismatch",
        "nocur": "currency_mismatch",
    }


def test_zero_competitor_price_has_no_pct() -> None:
    r = compute_intelligence(
        own_price=D("10"), map_price=None, currency="USD", competitors=[_comp("z", "0")]
    )
    assert r.comparisons[0].price_gap_pct is None


def test_map_violations() -> None:
    r = compute_intelligence(
        own_price=D("50"),
        map_price=D("60"),
        currency="USD",
        competitors=[_comp("below", "55"), _comp("equal", "60"), _comp("above", "70")],
    )
    flags = {c.match_id: c.map_violation for c in r.comparisons}
    assert flags == {"below": True, "equal": False, "above": False}
    assert r.comparisons[0].map_gap == D("5.00")
    assert r.map_violation_count == 1
    assert r.own_price_below_map is True


def test_no_map_price_means_no_violation() -> None:
    r = compute_intelligence(
        own_price=D("1"), map_price=None, currency="USD", competitors=[_comp("a", "0.5")]
    )
    assert r.map_violation_count == 0 and r.own_price_below_map is False


def _base(**over: object) -> dict[str, object]:
    return {"name": " Widget ", "own_price": "9.99", "currency": "usd", **over}


def test_create_request_normalizes() -> None:
    req = CreateInternalProductRequest(**_base(sku=" ab-1 ", barcode="012345678905"))  # type: ignore[arg-type]
    assert (req.name, req.sku, req.currency) == ("Widget", "ab-1", "USD")


@pytest.mark.parametrize(
    "bad",
    [
        {"own_price": "-1"},
        {"own_price": "1.234"},
        {"own_price": "NaN"},
        {"own_price": "Infinity"},
        {"own_price": "12345678901"},
        {"map_price": "-0.01"},
        {"currency": "US"},
        {"currency": "US1"},
        {"barcode": "123"},
        {"barcode": "12345678a"},
        {"name": "   "},
        {"sku": "  "},
        {"status": "deleted"},
        {"unknown": 1},
    ],
)
def test_create_request_rejects_invalid(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CreateInternalProductRequest(**_base(**bad))  # type: ignore[arg-type]


def test_patch_request_rejects_unknown_and_bad_values() -> None:
    with pytest.raises(ValidationError):
        PatchInternalProductRequest(workspace_id="x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        PatchInternalProductRequest(own_price="-5")  # type: ignore[arg-type]
    assert PatchInternalProductRequest(map_price=None).model_dump(exclude_unset=True) == {
        "map_price": None
    }


def test_match_request_requires_exactly_one_reference() -> None:
    iid = "00000000-0000-0000-0000-000000000001"
    pid = "00000000-0000-0000-0000-000000000002"
    with pytest.raises(ValidationError):
        CreateMatchRequest(internal_product_id=iid)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        CreateMatchRequest(internal_product_id=iid, product_id=pid, product_url="http://x.test/a")  # type: ignore[arg-type]
    assert CreateMatchRequest(internal_product_id=iid, product_id=pid).product_id is not None  # type: ignore[arg-type]
