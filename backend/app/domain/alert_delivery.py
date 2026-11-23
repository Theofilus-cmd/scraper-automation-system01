"""Email delivery for alerts: claim -> send -> mark.

Idempotency / duplicate protection:
  * Alert rows are unique per (task, product, kind) (see alert_detection),
    so a retried scrape task cannot create a second alert.
  * Delivery *claims* rows with `FOR UPDATE SKIP LOCKED` and flips them to
    'sending' before any SMTP I/O, so two workers never send the same alert.
  * Only 'sent' / 'skipped' / 'failed' (attempts exhausted) are terminal; a
    failed send returns the row to 'pending' for a later retry.
  * Residual window: a worker that dies between SMTP success and the 'sent'
    update leaves a stale 'sending' claim that is re-claimed after
    `alert_claim_timeout_seconds` (at-least-once, never silently lost). The
    Message-ID is the alert id so a duplicate is at least recognizable.
"""

from __future__ import annotations

import asyncio
import smtplib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage

from sqlalchemy import select, text, update

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.alerts import Alert
from app.db.models.identity import User, WorkspaceMember
from app.db.session import get_session
from app.domain.alert_detection import PRICE_CHANGED, STOCK_STATUS_CHANGED

logger = get_logger(__name__)

# Roles that receive alert mail; viewers are read-only and excluded.
RECIPIENT_ROLES = ("owner", "admin", "member")

# Sender signature: (message, settings) -> None; raises on failure.
EmailSender = Callable[[EmailMessage, Settings], None]


@dataclass(frozen=True, slots=True)
class DeliveryStats:
    claimed: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    retry_later: int = 0


def smtp_send(message: EmailMessage, settings: Settings) -> None:
    """Blocking SMTP send (MailHog in dev). Raises on any failure."""
    with smtplib.SMTP(
        settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
    ) as smtp:
        if settings.smtp_use_starttls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


def render_alert_email(
    alert: Alert, recipients: list[str], settings: Settings
) -> EmailMessage:
    cur = f" {alert.currency}" if alert.currency else ""
    if alert.kind == PRICE_CHANGED:
        subject = f"Price changed: {alert.product_name}"
        detail = f"Price: {alert.old_value}{cur} -> {alert.new_value}{cur}"
    elif alert.kind == STOCK_STATUS_CHANGED:
        subject = f"Stock status changed: {alert.product_name}"
        detail = f"Stock status: {alert.old_value} -> {alert.new_value}"
    else:  # pragma: no cover - guarded by DB CHECK constraint
        subject = f"Alert: {alert.product_name}"
        detail = f"{alert.old_value} -> {alert.new_value}"

    msg = EmailMessage()
    msg["From"] = settings.notification_from_email
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Message-ID"] = f"<alert-{alert.id}@scraper.local>"
    msg.set_content(f"{alert.product_name}\n{detail}\n\nProduct: {alert.product_url}\n")
    return msg


async def _claim_alerts(
    limit: int, settings: Settings, workspace_id: uuid.UUID | None
) -> list[uuid.UUID]:
    async with get_session() as session:
        rows = await session.execute(
            text(
                """
                UPDATE alerts SET email_status = 'sending',
                       attempts = attempts + 1, claimed_at = now()
                WHERE id IN (
                    SELECT id FROM alerts
                    WHERE (email_status = 'pending'
                           OR (email_status = 'sending'
                               AND claimed_at < now() - make_interval(secs => :timeout)))
                      AND attempts < :max_attempts
                      AND (CAST(:workspace_id AS uuid) IS NULL
                           OR workspace_id = CAST(:workspace_id AS uuid))
                    ORDER BY created_at
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id
                """
            ),
            {
                "timeout": settings.alert_claim_timeout_seconds,
                "max_attempts": settings.alert_max_delivery_attempts,
                "limit": limit,
                "workspace_id": workspace_id,
            },
        )
        ids = [r.id for r in rows]
        await session.commit()
        return ids


async def _recipients(workspace_id: uuid.UUID) -> list[str]:
    async with get_session() as session:
        rows = await session.execute(
            select(User.email)
            .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role.in_(RECIPIENT_ROLES),
                User.is_active.is_(True),
            )
            .order_by(User.email)
        )
        return list(rows.scalars())


async def _finish(
    alert_id: uuid.UUID, *, status: str, error: str | None = None, sent: bool = False
) -> None:
    async with get_session() as session:
        values: dict[str, object] = {"email_status": status, "last_error": error}
        if sent:
            values["sent_at"] = text("now()")
        await session.execute(update(Alert).where(Alert.id == alert_id).values(**values))
        await session.commit()


async def deliver_pending_alerts(
    *,
    sender: EmailSender | None = None,
    limit: int = 50,
    workspace_id: uuid.UUID | None = None,
) -> DeliveryStats:
    """Claim and email pending alerts (optionally one workspace only).

    Never raises for per-alert failures.
    """
    settings = get_settings()
    if not settings.alerts_email_enabled:
        return DeliveryStats()
    send = sender or smtp_send

    ids = await _claim_alerts(limit, settings, workspace_id)
    sent = skipped = failed = retry = 0

    for alert_id in ids:
        attempts = 0  # reset per alert; the claim already incremented it
        try:
            async with get_session() as session:
                alert = (
                    await session.execute(select(Alert).where(Alert.id == alert_id))
                ).scalar_one()
            attempts = alert.attempts
            recipients = await _recipients(alert.workspace_id)
            if not recipients:
                await _finish(alert_id, status="skipped", error="no recipients")
                skipped += 1
                continue
            message = render_alert_email(alert, recipients, settings)
            await asyncio.to_thread(send, message, settings)
        except Exception as exc:
            reason = f"{exc.__class__.__name__}: {exc}"[:500]
            logger.warning(
                "alert email delivery failed",
                extra={"alert_id": str(alert_id), "error": reason},
            )
            if attempts >= settings.alert_max_delivery_attempts:
                await _finish(alert_id, status="failed", error=reason)
                failed += 1
            else:
                await _finish(alert_id, status="pending", error=reason)
                retry += 1
            continue
        await _finish(alert_id, status="sent", sent=True)
        sent += 1

    return DeliveryStats(
        claimed=len(ids), sent=sent, skipped=skipped, failed=failed, retry_later=retry
    )
