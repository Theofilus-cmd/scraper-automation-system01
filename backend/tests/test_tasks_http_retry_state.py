"""Retry-state guards without a real database."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.workers import tasks_http


@pytest.mark.parametrize(
    "status",
    ["succeeded", "failed", "dead_letter", "in_progress"],
)
async def test_mark_retrying_preserves_terminal_tasks(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    task = SimpleNamespace(
        status=status,
        error_reason=None,
        error_detail=None,
    )
    result = MagicMock()
    result.scalar_one.return_value = task

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(tasks_http, "get_session", lambda: context)

    from datetime import UTC, datetime

    fixed_now = datetime(2026, 10, 7, 6, 0, tzinfo=UTC)
    monkeypatch.setattr(tasks_http, "_utcnow", lambda: fixed_now)

    changed = await tasks_http._mark_task_retrying(
        uuid.uuid4(),
        reason="network_error",
        message="retry test",
        retry_delay=60,
    )

    if status == "in_progress":
        assert changed is True
        assert task.status == "retrying"
        assert task.error_reason == "network_error"
        assert task.error_detail == {
            "message": "retry test",
            "retry_not_before": "2026-10-07T06:01:00+00:00",
        }
        assert task.queued_at == fixed_now
        session.commit.assert_awaited_once()
    else:
        assert changed is False
        assert task.status == status
        assert task.error_reason is None
        assert task.error_detail is None
        session.commit.assert_not_awaited()


def test_entry_point_does_not_retry_an_already_finalized_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers.async_bridge import run_async

    task_id = uuid.uuid4()

    async def fail(*args: object, **kwargs: object) -> dict[str, object]:
        raise tasks_http.TransientScrapeError("network_error", "fetch failed")

    async def already_finalized(
        received_id: uuid.UUID,
        *,
        reason: str,
        message: str,
        retry_delay: float,
    ) -> bool:
        assert received_id == task_id
        assert reason == "network_error"
        assert message == "fetch failed"
        assert retry_delay == float(tasks_http.scrape_source_url.default_retry_delay)
        return False

    def forbidden_retry(*args: object, **kwargs: object) -> None:
        raise AssertionError("terminal task must not publish a retry")

    monkeypatch.setattr(tasks_http, "run_async", run_async)
    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task_with_lease", fail)
    monkeypatch.setattr(tasks_http, "_mark_task_retrying", already_finalized)
    monkeypatch.setattr(tasks_http.scrape_source_url, "retry", forbidden_retry)

    result = tasks_http.scrape_source_url(str(task_id))

    assert result == {"status": "already_finalized"}


@pytest.mark.parametrize("lost_owner", [False, True])
def test_coordination_failure_defers_without_mutating_task_state(
    monkeypatch: pytest.MonkeyPatch,
    lost_owner: bool,
) -> None:
    from app.workers.coordination import (
        CoordinationUnavailableError,
        LeaseOwnershipLostError,
    )

    task_id = uuid.uuid4()
    error = (
        LeaseOwnershipLostError("ownership lost")
        if lost_owner
        else CoordinationUnavailableError("Redis unavailable")
    )

    async def fail(*args: object, **kwargs: object) -> dict[str, object]:
        raise error

    async def forbidden_state_write(*args: object, **kwargs: object) -> bool:
        raise AssertionError("coordination failure must not overwrite task state")

    def forbidden_retry(*args: object, **kwargs: object) -> None:
        raise AssertionError("coordination failure must not consume fetch retries")

    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task_with_lease", fail)
    monkeypatch.setattr(tasks_http, "_mark_task_retrying", forbidden_state_write)
    monkeypatch.setattr(tasks_http, "_finalize_task_dead_letter", forbidden_state_write)
    monkeypatch.setattr(tasks_http.scrape_source_url, "retry", forbidden_retry)

    result = tasks_http.scrape_source_url(str(task_id))

    assert result == {
        "status": "deferred",
        "reason": "lease_lost" if lost_owner else "coordination_unavailable",
    }


def test_exhausted_fetch_budget_finalizes_dead_letter_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any, cast

    task_id = uuid.uuid4()
    run_id = uuid.uuid4()
    finalized: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    task = cast(Any, tasks_http.scrape_source_url)

    async def fail(*args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs["attempt"] == 3
        raise tasks_http.TransientScrapeError("network_error", "fetch failed")

    async def mark_retrying(*args: object, **kwargs: object) -> bool:
        return True

    async def context(received_id: uuid.UUID) -> SimpleNamespace:
        assert received_id == task_id
        return SimpleNamespace(run_id=run_id)

    async def dead_letter(
        received_id: uuid.UUID,
        received_run_id: uuid.UUID,
        *,
        reason: str,
        message: str,
    ) -> None:
        finalized.append((received_id, received_run_id, reason))

    def forbidden_retry(*args: object, **kwargs: object) -> None:
        raise AssertionError("exhausted fetch budget must finalize before self.retry")

    monkeypatch.setattr(tasks_http, "_scrape_source_url_for_task_with_lease", fail)
    monkeypatch.setattr(tasks_http, "_mark_task_retrying", mark_retrying)
    monkeypatch.setattr(tasks_http, "_load_task_context", context)
    monkeypatch.setattr(tasks_http, "_finalize_task_dead_letter", dead_letter)
    monkeypatch.setattr(task, "retry", forbidden_retry)

    task.push_request(retries=2, called_directly=False)
    try:
        result = task.run(str(task_id))
    finally:
        task.pop_request()

    assert result == {
        "status": "failed",
        "error_reason": "network_error",
        "dead_letter": True,
    }
    assert finalized == [(task_id, run_id, "network_error")]
