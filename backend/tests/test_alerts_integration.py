"""Alert creation on ingest, idempotent delivery, and MailHog end-to-end.

    docker compose exec api pytest -m integration tests/test_alerts_integration.py

Each test uses its own isolated workspace and passes `workspace_id` to the
delivery sweep so it never claims alerts belonging to other tests.
"""

import asyncio
import json
import urllib.request
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select, update

from app.core.config import Settings, get_settings
from app.db.models.alerts import Alert
from app.db.models.repository import ScrapeUpsertResult, _record_alerts, upsert_scrape_result
from app.db.models.scraping import Source
from app.db.session import get_session
from app.domain.alert_delivery import deliver_pending_alerts, smtp_send
from app.scraping.types import NormalizedRecord, ValidationResult
from tests.factories import (
    create_isolated_workspace_source,
    create_test_run,
    create_test_task,
    finalize_run_and_task,
)

pytestmark = pytest.mark.integration


def _record(url: str, *, price: str, stock: str, sku: str = "ALERT-SKU") -> NormalizedRecord:
    return NormalizedRecord(
        product_url=url,
        scraped_at=datetime.now(UTC),
        stock_status=stock,
        product_name="Alert Widget",
        price=Decimal(price),
        currency="USD",
        sku=sku,
    )


async def _ingest(
    source: Source,
    *,
    price: str,
    stock: str = "in_stock",
    ids: tuple[uuid.UUID, uuid.UUID] | None = None,
) -> ScrapeUpsertResult:
    """One scrape write with its own run+task (finalized afterwards, since a
    source may only have one in-flight run). `ids=(run_id, task_id)` reuses
    an existing pair to simulate a redelivered task.
    """
    if ids is None:
        run = await create_test_run(source.id, status="running")
        task = await create_test_task(run.id, source.id)
        ids = (run.id, task.id)
    result = await upsert_scrape_result(
        source_id=source.id,
        run_id=ids[0],
        task_id=ids[1],
        record=_record(source.url, price=price, stock=stock),
        validation=ValidationResult(is_valid=True, errors={}),
    )
    await finalize_run_and_task(ids[0], ids[1])
    return result


async def _alerts(workspace_id: uuid.UUID) -> list[Alert]:
    async with get_session() as session:
        rows = await session.execute(
            select(Alert).where(Alert.workspace_id == workspace_id).order_by(Alert.created_at)
        )
        return list(rows.scalars())


class _Sender:
    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[EmailMessage] = []
        self.calls = 0
        self.fail_times = fail_times

    def __call__(self, message: EmailMessage, settings: Settings) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("smtp down")
        self.sent.append(message)


# --- detection on ingest ---------------------------------------------------


async def test_first_sighting_creates_no_alert() -> None:
    ws, source = await create_isolated_workspace_source()
    result = await _ingest(source, price="10.00")
    assert result.alerts_created == 0
    assert await _alerts(ws.id) == []


async def test_unchanged_rescrape_creates_no_alert() -> None:
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    result = await _ingest(source, price="10.00")
    assert result.alerts_created == 0
    assert await _alerts(ws.id) == []


async def test_price_change_creates_one_alert() -> None:
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    result = await _ingest(source, price="8.50")
    assert result.alerts_created == 1
    (alert,) = await _alerts(ws.id)
    assert alert.kind == "price_changed"
    assert (alert.old_value, alert.new_value, alert.currency) == ("10.00", "8.50", "USD")
    assert alert.email_status == "pending"
    assert alert.source_id == source.id


async def test_stock_change_creates_alert_and_both_changes_create_two() -> None:
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00", stock="in_stock")
    await _ingest(source, price="10.00", stock="out_of_stock")
    await _ingest(source, price="12.00", stock="in_stock")
    kinds = sorted(a.kind for a in await _alerts(ws.id))
    assert kinds == ["price_changed", "stock_status_changed", "stock_status_changed"]


async def test_replayed_write_does_not_duplicate_alert() -> None:
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    run = await create_test_run(source.id, status="running")
    task = await create_test_task(run.id, source.id)
    first = await _ingest(source, price="9.00", ids=(run.id, task.id))
    assert first.product_id is not None
    assert first.alerts_created == 1

    # Simulate a redelivered task whose diff is recomputed against the old
    # observation: the same (task, product, kind) must not insert again.
    async with get_session() as session:
        again = await _record_alerts(
            session,
            source_id=source.id,
            product_id=first.product_id,
            run_id=run.id,
            task_id=task.id,
            change_summary={"price": {"from": "10.00", "to": "9.00"}},
            product_name="Alert Widget",
            product_url=source.url,
            currency="USD",
        )
        await session.commit()
    assert again == 0
    assert len(await _alerts(ws.id)) == 1


# --- delivery ----------------------------------------------------------------


async def test_delivery_sends_once_then_is_idempotent() -> None:
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    await _ingest(source, price="7.00")

    sender = _Sender()
    stats = await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    assert (stats.claimed, stats.sent) == (1, 1)
    msg = sender.sent[0]
    assert "Price changed" in msg["Subject"]
    assert "10.00 USD -> 7.00 USD" in msg.get_content()

    again = await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    assert again.claimed == 0
    assert len(sender.sent) == 1
    (alert,) = await _alerts(ws.id)
    assert alert.email_status == "sent" and alert.sent_at is not None


async def test_concurrent_sweeps_send_each_alert_exactly_once() -> None:
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    await _ingest(source, price="7.00")

    sender = _Sender()
    await asyncio.gather(
        *(deliver_pending_alerts(sender=sender, workspace_id=ws.id) for _ in range(5))
    )
    assert len(sender.sent) == 1


async def test_failed_send_is_retried_and_not_lost() -> None:
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    await _ingest(source, price="7.00")

    sender = _Sender(fail_times=1)
    first = await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    assert (first.sent, first.retry_later) == (0, 1)
    (alert,) = await _alerts(ws.id)
    assert alert.email_status == "pending" and "smtp down" in (alert.last_error or "")

    second = await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    assert second.sent == 1
    assert len(sender.sent) == 1


async def test_attempts_exhausted_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "alert_max_delivery_attempts", 2)
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    await _ingest(source, price="7.00")

    sender = _Sender(fail_times=99)
    await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    last = await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    assert last.failed == 1
    again = await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    assert again.claimed == 0
    assert (await _alerts(ws.id))[0].email_status == "failed"


async def test_stale_sending_claim_is_reclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    await _ingest(source, price="7.00")
    async with get_session() as session:
        await session.execute(
            update(Alert)
            .where(Alert.workspace_id == ws.id)
            .values(email_status="sending", attempts=1, claimed_at=func.now())
        )
        await session.commit()

    sender = _Sender()
    fresh = await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    assert fresh.claimed == 0  # live claim by another worker is respected

    monkeypatch.setattr(get_settings(), "alert_claim_timeout_seconds", 0)
    stale = await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    assert stale.sent == 1


async def test_no_recipients_is_skipped() -> None:
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    await _ingest(source, price="7.00")
    async with get_session() as session:
        from app.db.models.identity import WorkspaceMember

        await session.execute(
            update(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == ws.id)
            .values(role="viewer")
        )
        await session.commit()

    sender = _Sender()
    stats = await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    assert (stats.skipped, len(sender.sent)) == (1, 0)


async def test_disabled_email_leaves_alerts_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "alerts_email_enabled", False)
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    await _ingest(source, price="7.00")
    sender = _Sender()
    stats = await deliver_pending_alerts(sender=sender, workspace_id=ws.id)
    assert stats.claimed == 0 and sender.calls == 0
    assert (await _alerts(ws.id))[0].email_status == "pending"


# --- MailHog end to end --------------------------------------------------------


def _mailhog_messages() -> list[dict[str, object]]:
    host = get_settings().smtp_host
    with urllib.request.urlopen(f"http://{host}:8025/api/v2/messages?limit=200", timeout=5) as r:
        items: list[dict[str, object]] = json.load(r)["items"]
        return items


def _message_ids(message: dict[str, object]) -> list[str]:
    content = message["Content"]
    assert isinstance(content, dict)
    ids: list[str] = content["Headers"].get("Message-ID", [])
    return ids


async def test_email_reaches_mailhog_exactly_once() -> None:
    ws, source = await create_isolated_workspace_source()
    await _ingest(source, price="10.00")
    await _ingest(source, price="7.00")
    (alert,) = await _alerts(ws.id)
    message_id = f"<alert-{alert.id}@scraper.local>"

    await deliver_pending_alerts(sender=smtp_send, workspace_id=ws.id)
    await deliver_pending_alerts(sender=smtp_send, workspace_id=ws.id)

    matches = [
        m
        for m in await asyncio.to_thread(_mailhog_messages)
        if message_id in _message_ids(m)
    ]
    assert len(matches) == 1
