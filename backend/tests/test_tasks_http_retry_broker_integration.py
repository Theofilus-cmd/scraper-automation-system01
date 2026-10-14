"""Retry through the real broker, HTTP worker, and mock-store."""

import time
import uuid

import pytest
from sqlalchemy import func, select

from app.db.models.lifecycle import ObservationHistory, Run, Task
from app.db.session import get_session
from app.workers.async_bridge import run_async
from app.workers.tasks_http import scrape_source_url
from tests.factories import (
    MOCK_STORE_BASE_URL,
    create_test_run,
    create_test_source,
    create_test_task,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("scenario", "expected_state"),
    [
        pytest.param(
            "retry-test",
            ("succeeded", 2, "completed", 1, 0, 1),
            id="503-then-success",
        ),
        pytest.param(
            "dead-letter-test",
            ("dead_letter", 3, "failed", 0, 1, 0),
            id="persistent-503-dead-letter",
        ),
    ],
)
def test_real_broker_retry_outcomes(
    scenario: str,
    expected_state: tuple[str, int, str, int, int, int],
) -> None:
    async def setup() -> tuple[uuid.UUID, uuid.UUID]:
        token = uuid.uuid4().hex
        source = await create_test_source(
            url=f"{MOCK_STORE_BASE_URL}/products/{scenario}-{token}"
        )
        run = await create_test_run(source.id)
        task = await create_test_task(run.id, source.id)
        return run.id, task.id

    async def durable_state(
        run_id: uuid.UUID,
        task_id: uuid.UUID,
    ) -> tuple[str, int, str, int, int, int]:
        async with get_session() as session:
            task = (
                await session.execute(select(Task).where(Task.id == task_id))
            ).scalar_one()
            run = (
                await session.execute(select(Run).where(Run.id == run_id))
            ).scalar_one()
            history_count = await session.scalar(
                select(func.count())
                .select_from(ObservationHistory)
                .where(ObservationHistory.task_id == task_id)
            )
            return (
                task.status,
                task.attempt_count,
                run.status,
                run.succeeded_tasks,
                run.failed_tasks,
                int(history_count or 0),
            )

    run_id, task_id = run_async(setup())
    scrape_source_url.delay(str(task_id))

    deadline = time.monotonic() + 60
    last_state = None
    while time.monotonic() < deadline:
        last_state = run_async(durable_state(run_id, task_id))
        if last_state[0] in ("succeeded", "failed", "dead_letter"):
            break
        time.sleep(0.25)
    else:
        raise AssertionError(
            f"Broker retry did not finish within 60 seconds: {last_state!r}"
        )

    assert last_state == expected_state
