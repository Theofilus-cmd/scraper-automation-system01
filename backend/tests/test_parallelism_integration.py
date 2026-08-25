"""doc 18 §8.4: parallelism tests against a real Postgres/Redis -- proving
invariants hold under genuine concurrent load (asyncio.gather of several
real calls, each opening its own DB connection), not a serialized
simulation of concurrency.

Scope note on doc 18 §8.4's fifth item, "task redelivery / idempotent
claim" (doc 07 §4): that guarantee rests on tasks.idempotency_key's UNIQUE
constraint plus upsert semantics, not on anything reachable without a real
multi-worker Celery redelivery -- not independently re-simulated here
beyond the idempotency-key coverage already in
test_run_lifecycle_integration.py, flagged rather than silently skipped.

    docker compose exec api pytest -m integration
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models.lifecycle import Run
from app.db.models.repository import upsert_scrape_result
from app.db.models.runs_repository import claim_due_schedules, create_manual_run
from app.db.models.scraping import CurrentObservation, Product
from app.db.models.sources_repository import upsert_schedule
from app.db.session import get_session
from app.domain.errors import RunInProgressError
from app.scraping.types import NormalizedRecord, ValidationResult
from tests.factories import create_test_run, create_test_source, create_test_task

pytestmark = pytest.mark.integration


async def _force_schedule_due(source_id: uuid.UUID) -> None:
    async with get_session() as session:
        schedule = await upsert_schedule(session, source_id, interval_minutes=15, is_active=True)
        schedule.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()


async def test_concurrent_scheduler_claims_never_double_fire() -> None:
    """doc 18 §4.2/§8.4: two claim iterations run concurrently against N
    due, unblocked schedules -> exactly N runs, never 2N. `FOR UPDATE ...
    SKIP LOCKED` (doc 18 §4.1) is the actual mechanism; this proves it
    against real concurrent transactions, not a serialized simulation.
    """
    sources = [await create_test_source() for _ in range(5)]
    for source in sources:
        await _force_schedule_due(source.id)

    claimed_counts = await asyncio.gather(
        claim_due_schedules(batch_size=20), claim_due_schedules(batch_size=20)
    )

    assert sum(claimed_counts) == len(sources)
    async with get_session() as session:
        for source in sources:
            runs = (
                await session.execute(select(Run).where(Run.source_id == source.id))
            ).scalars().all()
            assert len(runs) == 1


async def test_no_overlap_invariant_under_real_concurrent_manual_triggers() -> None:
    """doc 18 §4.4/§8.4 (amendment 2's explicit ask for a real concurrency
    proof): several concurrent create_manual_run() calls for the SAME
    source, no idempotency key -> exactly one succeeds, every other one
    observes RunInProgressError referencing that same winning run.
    """
    source = await create_test_source()

    async def _attempt() -> Run | RunInProgressError:
        try:
            run, _task, _created = await create_manual_run(
                source_id=source.id, idempotency_key=None, attach_to_in_flight=False
            )
            return run
        except RunInProgressError as exc:
            return exc

    results = await asyncio.gather(*(_attempt() for _ in range(8)))

    successes = [r for r in results if isinstance(r, Run)]
    collisions = [r for r in results if isinstance(r, RunInProgressError)]

    assert len(successes) == 1
    assert len(collisions) == 7
    winning_run_id = successes[0].id
    assert all(exc.existing_run_id == winning_run_id for exc in collisions)

    async with get_session() as session:
        runs = (
            await session.execute(select(Run).where(Run.source_id == source.id))
        ).scalars().all()
    assert len(runs) == 1


async def test_scheduler_claim_races_a_manual_trigger_for_the_same_source() -> None:
    """doc 18 §8.4: a schedule becomes due for source X at the same moment
    a manual trigger fires for X -> exactly one run is created between the
    two; the loser observes the collision through its own path (the
    scheduler simply claims nothing for X, retried next cycle per §4.1;
    the manual caller sees RunInProgressError).
    """
    source = await create_test_source()
    await _force_schedule_due(source.id)

    async def _manual_attempt() -> Run | RunInProgressError:
        try:
            run, _task, _created = await create_manual_run(
                source_id=source.id, idempotency_key=None, attach_to_in_flight=False
            )
            return run
        except RunInProgressError as exc:
            return exc

    manual_result, _claimed = await asyncio.gather(
        _manual_attempt(), claim_due_schedules(batch_size=20)
    )

    async with get_session() as session:
        runs = (
            await session.execute(select(Run).where(Run.source_id == source.id))
        ).scalars().all()
    # Exactly one run exists for this source regardless of which side won.
    assert len(runs) == 1
    if isinstance(manual_result, RunInProgressError):
        assert manual_result.existing_run_id == runs[0].id
    else:
        assert manual_result.id == runs[0].id


async def test_concurrent_writes_to_the_same_product_identity_stay_coherent() -> None:
    """doc 18 §3.3/§8.4: several genuinely concurrent scrapes of the same
    product identity -> no corrupted/duplicated row, and a coherent (if
    not caller-predictable) final state. The FOR UPDATE-serialized diff
    (repository.py::_diff_against_prior_observation) is what makes this
    safe; this proves it under real concurrency rather than assuming it
    from the code alone.

    Each concurrent "scrape" gets its own run+task, seeded directly as
    `status="completed"` (not the factory's "running" default) precisely
    so this test exercises only the product/observation upsert race, not
    the no-overlap-runs invariant (already covered by the tests above) --
    six simultaneous "running" runs for one source would themselves
    collide on uq_runs_one_in_flight_per_source, which is not what this
    test is about.
    """
    source = await create_test_source()
    prices = [Decimal(f"{10 + i}.99") for i in range(6)]

    async def _scrape(price: Decimal) -> None:
        run = await create_test_run(source.id, status="completed")
        task = await create_test_task(run.id, source.id, status="succeeded")
        record = NormalizedRecord(
            product_url=source.url,
            scraped_at=datetime.now(UTC),
            stock_status="in_stock",
            product_name="Concurrency Test Widget",
            price=price,
            currency="USD",
            sku="CONCURRENCY-TEST-SKU",
        )
        await upsert_scrape_result(
            source_id=source.id,
            run_id=run.id,
            task_id=task.id,
            record=record,
            validation=ValidationResult(is_valid=True, errors={}),
        )

    await asyncio.gather(*(_scrape(price) for price in prices))

    async with get_session() as session:
        products = (
            await session.execute(select(Product).where(Product.source_id == source.id))
        ).scalars().all()
        assert len(products) == 1  # exactly one product identity, no duplicate row

        observations = (
            await session.execute(
                select(CurrentObservation).where(CurrentObservation.product_id == products[0].id)
            )
        ).scalars().all()

    assert len(observations) == 1  # exactly one current_observations row, never corrupted
    # last-write-wins is accepted and not caller-predictable (doc 18 §3.3's
    # accepted limitation) -- only coherence is asserted, not which write won.
    assert observations[0].price in prices
