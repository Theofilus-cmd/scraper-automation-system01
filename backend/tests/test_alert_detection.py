"""Unit tests for pure alert detection (no database)."""

import uuid

from app.domain.alert_detection import (
    PRICE_CHANGED,
    STOCK_STATUS_CHANGED,
    build_dedupe_key,
    detect_alerts,
)


def test_none_and_empty_summary_yield_no_alerts() -> None:
    assert detect_alerts(None, currency="USD") == []
    assert detect_alerts({}, currency="USD") == []


def test_price_change_detected_with_currency() -> None:
    drafts = detect_alerts({"price": {"from": "19.99", "to": "14.99"}}, currency="USD")
    assert len(drafts) == 1
    assert drafts[0].kind == PRICE_CHANGED
    assert (drafts[0].old_value, drafts[0].new_value, drafts[0].currency) == (
        "19.99",
        "14.99",
        "USD",
    )


def test_numerically_equal_price_is_not_an_alert() -> None:
    assert detect_alerts({"price": {"from": "10.0", "to": "10.00"}}, currency="USD") == []


def test_unparseable_price_is_ignored() -> None:
    assert detect_alerts({"price": {"from": "abc", "to": "10.00"}}, currency="USD") == []


def test_stock_status_change_detected() -> None:
    drafts = detect_alerts(
        {"stock_status": {"from": "in_stock", "to": "out_of_stock"}}, currency="USD"
    )
    assert [(d.kind, d.old_value, d.new_value) for d in drafts] == [
        (STOCK_STATUS_CHANGED, "in_stock", "out_of_stock")
    ]


def test_both_changes_yield_two_alerts() -> None:
    drafts = detect_alerts(
        {
            "price": {"from": "5", "to": "6"},
            "stock_status": {"from": "unknown", "to": "in_stock"},
        },
        currency="EUR",
    )
    assert {d.kind for d in drafts} == {PRICE_CHANGED, STOCK_STATUS_CHANGED}


def test_untracked_field_changes_do_not_alert() -> None:
    summary = {"rating": {"from": "4.0", "to": "4.5"}, "description": {"from": "a", "to": "b"}}
    assert detect_alerts(summary, currency="USD") == []


def test_malformed_entries_are_ignored() -> None:
    assert detect_alerts({"price": "oops", "stock_status": {"from": "x"}}, currency="USD") == []


def test_dedupe_key_is_stable_and_scoped() -> None:
    task, product = uuid.uuid4(), uuid.uuid4()
    k1 = build_dedupe_key(task_id=task, product_id=product, kind=PRICE_CHANGED)
    assert k1 == build_dedupe_key(task_id=task, product_id=product, kind=PRICE_CHANGED)
    assert k1 != build_dedupe_key(task_id=task, product_id=product, kind=STOCK_STATUS_CHANGED)
    assert k1 != build_dedupe_key(task_id=uuid.uuid4(), product_id=product, kind=PRICE_CHANGED)
