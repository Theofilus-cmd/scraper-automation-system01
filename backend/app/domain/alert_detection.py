"""Pure change detection for product alerts.

Input is the `change_summary` diff that `repository.py` already computes
against the prior `current_observations` row (`{field: {"from", "to"}}`,
values JSON-safe). A first sighting has no summary and therefore never
alerts: there is nothing to compare against.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

PRICE_CHANGED = "price_changed"
STOCK_STATUS_CHANGED = "stock_status_changed"


@dataclass(frozen=True, slots=True)
class AlertDraft:
    kind: str
    old_value: str
    new_value: str
    currency: str | None = None


def build_dedupe_key(*, task_id: uuid.UUID | None, product_id: uuid.UUID, kind: str) -> str:
    """One alert per (task, product, kind): redelivered tasks cannot duplicate."""
    return f"{task_id or 'no-task'}:{product_id}:{kind}"


def _as_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _entry(summary: Mapping[str, object], field: str) -> tuple[object, object] | None:
    raw = summary.get(field)
    if not isinstance(raw, Mapping) or "from" not in raw or "to" not in raw:
        return None
    return raw["from"], raw["to"]


def detect_alerts(
    change_summary: Mapping[str, object] | None, *, currency: str | None
) -> list[AlertDraft]:
    """Return the alert drafts implied by a diff (empty if none)."""
    if not change_summary:
        return []

    drafts: list[AlertDraft] = []

    price = _entry(change_summary, "price")
    if price is not None:
        old, new = _as_decimal(price[0]), _as_decimal(price[1])
        if old is not None and new is not None and old != new:
            drafts.append(
                AlertDraft(
                    kind=PRICE_CHANGED,
                    old_value=str(price[0]),
                    new_value=str(price[1]),
                    currency=currency,
                )
            )

    stock = _entry(change_summary, "stock_status")
    if stock is not None and stock[0] != stock[1]:
        drafts.append(
            AlertDraft(
                kind=STOCK_STATUS_CHANGED,
                old_value=str(stock[0]),
                new_value=str(stock[1]),
            )
        )

    return drafts
