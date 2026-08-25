"""doc 18 §7.5/§8.2: retention purge integration tests against a real
Postgres -- end-to-end purge correctness (only eligible, terminal,
past-window rows are removed, in the correct child-before-parent order),
the immutability trigger's unconditional rejection of ordinary UPDATE/
DELETE, and confirmation the purge's own narrow bypass never leaks past
its own transaction.

    docker compose exec api pytest -m integration
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.models.lifecycle import ObservationHistory, Run, Task
from app.db.models.retention import purge_expired_history
from app.db.models.scraping import Product, Source
from app.db.session import get_session
from tests.factories import create_test_run, create_test_source, create_test_task

pytestmark = pytest.mark.integration


async def _create_product(source_id: uuid.UUID) -> Product:
    async with get_session() as session:
        product = Product(
            source_id=source_id,
            product_identity_key=f"sku:retention-test-{uuid.uuid4().hex}",
            product_url="http://mock-store:4000/products/retention-test",
        )
        session.add(product)
        await session.commit()
        return product


async def _create_history_row(
    product_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID,
    *,
    version_created_at: datetime,
) -> ObservationHistory:
    async with get_session() as session:
        row = ObservationHistory(
            product_id=product_id,
            run_id=run_id,
            task_id=task_id,
            product_name="Retention Test Widget",
            price=Decimal("9.99"),
            currency="USD",
            stock_status="in_stock",
            is_valid=True,
            scraped_at=datetime.now(UTC),
            version_created_at=version_created_at,
        )
        session.add(row)
        await session.commit()
        return row


async def _backdate_run_created_at(run_id: uuid.UUID, *, days_ago: int) -> None:
    async with get_session() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        run.created_at = datetime.now(UTC) - timedelta(days=days_ago)
        await session.commit()


async def _history_exists(row_id: uuid.UUID) -> bool:
    async with get_session() as session:
        result = await session.execute(
            select(ObservationHistory).where(ObservationHistory.id == row_id)
        )
        return result.scalar_one_or_none() is not None


async def _task_exists(row_id: uuid.UUID) -> bool:
    async with get_session() as session:
        result = await session.execute(select(Task).where(Task.id == row_id))
        return result.scalar_one_or_none() is not None


async def _run_exists(row_id: uuid.UUID) -> bool:
    async with get_session() as session:
        result = await session.execute(select(Run).where(Run.id == row_id))
        return result.scalar_one_or_none() is not None


async def _source_exists(row_id: uuid.UUID) -> bool:
    async with get_session() as session:
        result = await session.execute(select(Source).where(Source.id == row_id))
        return result.scalar_one_or_none() is not None


async def _product_exists(row_id: uuid.UUID) -> bool:
    async with get_session() as session:
        result = await session.execute(select(Product).where(Product.id == row_id))
        return result.scalar_one_or_none() is not None


async def test_purge_removes_only_eligible_rows_in_correct_order() -> None:
    # Scenario 1: old + terminal -- fully eligible.
    source1 = await create_test_source()
    product1 = await _create_product(source1.id)
    run1 = await create_test_run(source1.id, status="completed")
    await _backdate_run_created_at(run1.id, days_ago=100)
    task1 = await create_test_task(run1.id, source1.id, status="succeeded")
    history1 = await _create_history_row(
        product1.id, run1.id, task1.id, version_created_at=datetime.now(UTC) - timedelta(days=100)
    )

    # Scenario 2: recent -- must survive (not old enough).
    source2 = await create_test_source()
    product2 = await _create_product(source2.id)
    run2 = await create_test_run(source2.id, status="completed")
    task2 = await create_test_task(run2.id, source2.id, status="succeeded")
    history2 = await _create_history_row(
        product2.id, run2.id, task2.id, version_created_at=datetime.now(UTC)
    )

    # Scenario 3: old but still in flight -- must survive (doc 18 §7.5's
    # explicit "never purge history belonging to a still-in-flight run").
    source3 = await create_test_source()
    product3 = await _create_product(source3.id)
    run3 = await create_test_run(source3.id, status="running")
    await _backdate_run_created_at(run3.id, days_ago=100)
    task3 = await create_test_task(run3.id, source3.id, status="queued")
    history3 = await _create_history_row(
        product3.id, run3.id, task3.id, version_created_at=datetime.now(UTC) - timedelta(days=100)
    )

    result = await purge_expired_history(retention_days=90)
    assert result["observation_history_deleted"] >= 1
    assert result["tasks_deleted"] >= 1
    assert result["runs_deleted"] >= 1

    assert await _history_exists(history1.id) is False
    assert await _task_exists(task1.id) is False
    assert await _run_exists(run1.id) is False

    assert await _history_exists(history2.id) is True
    assert await _task_exists(task2.id) is True
    assert await _run_exists(run2.id) is True

    assert await _history_exists(history3.id) is True
    assert await _task_exists(task3.id) is True
    assert await _run_exists(run3.id) is True

    # current_observations/sources/products are current-state tables --
    # never touched by retention regardless of the above (doc 18 §7.5).
    assert await _source_exists(source1.id) is True
    assert await _product_exists(product1.id) is True


async def test_purge_bypass_does_not_leak_past_its_own_transaction() -> None:
    source = await create_test_source()
    product = await _create_product(source.id)
    run = await create_test_run(source.id, status="completed")
    await _backdate_run_created_at(run.id, days_ago=100)
    task = await create_test_task(run.id, source.id, status="succeeded")
    old_history = await _create_history_row(
        product.id, run.id, task.id, version_created_at=datetime.now(UTC) - timedelta(days=100)
    )
    recent_history = await _create_history_row(
        product.id, run.id, task.id, version_created_at=datetime.now(UTC)
    )

    result = await purge_expired_history(retention_days=90)
    assert result["observation_history_deleted"] >= 1
    assert await _history_exists(old_history.id) is False  # purge itself worked

    # Immediately after that transaction committed, a direct DELETE from a
    # fresh, ordinary transaction (no SET LOCAL) against the row that
    # survived is still rejected -- the bypass never leaks past the
    # purge's own transaction.
    with pytest.raises(DBAPIError):
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM observation_history WHERE id = :id"),
                {"id": recent_history.id},
            )
            await session.commit()

    assert await _history_exists(recent_history.id) is True


async def test_immutable_history_rejects_direct_update() -> None:
    source = await create_test_source()
    product = await _create_product(source.id)
    run = await create_test_run(source.id, status="completed")
    task = await create_test_task(run.id, source.id, status="succeeded")
    history = await _create_history_row(
        product.id, run.id, task.id, version_created_at=datetime.now(UTC)
    )

    with pytest.raises(DBAPIError):
        async with get_session() as session:
            await session.execute(
                text("UPDATE observation_history SET price = 1.00 WHERE id = :id"),
                {"id": history.id},
            )
            await session.commit()

    assert await _history_exists(history.id) is True  # untouched


async def test_immutable_history_rejects_direct_delete_outside_purge_bypass() -> None:
    source = await create_test_source()
    product = await _create_product(source.id)
    run = await create_test_run(source.id, status="completed")
    task = await create_test_task(run.id, source.id, status="succeeded")
    history = await _create_history_row(
        product.id, run.id, task.id, version_created_at=datetime.now(UTC)
    )

    with pytest.raises(DBAPIError):
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM observation_history WHERE id = :id"), {"id": history.id}
            )
            await session.commit()

    assert await _history_exists(history.id) is True  # untouched
