"""Worker-level handling of politeness deferrals (DB-free)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.workers import tasks_http


class FakeCeleryTask:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def apply_async(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_deferral_redispatches_with_incremented_count() -> None:
    task = FakeCeleryTask()
    tid = uuid.uuid4()
    out = tasks_http._redispatch_after_domain_deferral(
        task,  # type: ignore[arg-type]
        tid,
        {"status": "deferred_domain", "reason": "domain_cooldown", "delay": 42.0},
        defer_count=3,
    )
    assert out == {"status": "deferred", "reason": "domain_cooldown"}
    assert task.calls == [
        {"args": (str(tid),), "kwargs": {"defer_count": 4}, "countdown": 42.0}
    ]


def test_deferral_cap_dead_letters(monkeypatch: pytest.MonkeyPatch) -> None:
    task = FakeCeleryTask()
    finalized: list[str] = []
    run_id = uuid.uuid4()

    async def load(_tid: uuid.UUID) -> Any:
        return tasks_http._TaskContext(
            task_id=_tid, run_id=run_id, source_id=uuid.uuid4(),
            source_url="https://x.test/", source_status="active",
        )

    async def dead_letter(_t: uuid.UUID, _r: uuid.UUID, *, reason: str, message: str) -> None:
        finalized.append(reason)

    monkeypatch.setattr(tasks_http, "_load_task_context", load)
    monkeypatch.setattr(tasks_http, "_finalize_task_dead_letter", dead_letter)

    out = tasks_http._redispatch_after_domain_deferral(
        task,  # type: ignore[arg-type]
        uuid.uuid4(),
        {"status": "deferred_domain", "reason": "domain_busy", "delay": 1.0},
        defer_count=tasks_http.MAX_POLITENESS_DEFERRALS,
    )
    assert out["dead_letter"] is True
    assert finalized == ["network_error"]
    assert task.calls == []


@pytest.mark.integration
def test_cooldown_defers_real_task_without_spending_an_attempt() -> None:
    from sqlalchemy import select

    from app.db.models.lifecycle import Task
    from app.db.session import get_session
    from app.workers.async_bridge import run_async
    from app.workers.domain_cooldown import extend_domain_cooldown
    from app.workers.domain_rate_limit import get_domain_rate_redis_client
    from tests.factories import (
        MOCK_STORE_BASE_URL,
        create_test_run,
        create_test_source,
        create_test_task,
    )

    async def scenario() -> tuple[dict[str, Any], str, int]:
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/test-{uuid.uuid4().hex}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)
        client = get_domain_rate_redis_client()
        try:
            await extend_domain_cooldown("mock-store", client=client)
            result = await tasks_http._scrape_source_url_for_task(task.id, attempt=1)
        finally:
            await client.eval('return redis.call("DEL", KEYS[1])', 1,  # type: ignore[call-overload]
                              "scraper:domain-cooldown:mock-store")
            await client.aclose()
        async with get_session() as session:
            row = (await session.execute(select(Task).where(Task.id == task.id))).scalar_one()
            return result, row.status, row.attempt_count

    result, status, attempts = run_async(scenario())
    assert result["status"] == "deferred_domain"
    assert result["reason"] == "domain_cooldown"
    assert status == "queued"
    assert attempts == 0
