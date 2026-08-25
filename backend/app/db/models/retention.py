"""Retention purge (doc 18 §7.5, amendment 6): removes eligible historical
`observation_history`/`tasks`/`runs` rows older than
`settings.history_retention_days` (default 90). Never touches
`current_observations`, `sources`, or `products` -- those are current-state
tables, not history (doc 07 §7's reasoning, reused directly).

The `SET LOCAL app.allow_history_purge = 'true'` statement is the ONLY
sanctioned way anything ever deletes from `observation_history` --
migration 0003's trigger (`reject_observation_history_mutation`) rejects
every other UPDATE/DELETE against that table, unconditionally, for anyone.
`SET LOCAL` scopes the flag to exactly this transaction; it reverts
automatically at COMMIT, so it can never leak onto a later query on a
pooled PgBouncer connection (transaction-pooling mode hands out the same
underlying Postgres connection for exactly one transaction's duration,
which is precisely the scope `SET LOCAL` matches -- doc 13 §1).

Deletes run child-before-parent (observation_history -> tasks -> runs),
required by migration 0003's `RESTRICT` foreign keys regardless: an
attempt to delete a `runs` row while un-purged `tasks`/`observation_history`
still reference it is physically rejected by Postgres, not just by this
function remembering the right order (doc 18 §2.6).
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.session import get_session

logger = get_logger(__name__)


async def purge_expired_history(*, retention_days: int) -> dict[str, int]:
    async with get_session() as session:
        await session.execute(text("SET LOCAL app.allow_history_purge = 'true'"))

        history_result = await session.execute(
            text(
                """
                DELETE FROM observation_history
                WHERE version_created_at < now() - (:days || ' days')::interval
                  AND run_id NOT IN (
                      SELECT id FROM runs WHERE status IN ('pending', 'running')
                  )
                """
            ),
            {"days": retention_days},
        )
        tasks_result = await session.execute(
            text(
                """
                DELETE FROM tasks
                WHERE run_id IN (
                    SELECT id FROM runs
                    WHERE created_at < now() - (:days || ' days')::interval
                      AND status NOT IN ('pending', 'running')
                )
                """
            ),
            {"days": retention_days},
        )
        runs_result = await session.execute(
            text(
                """
                DELETE FROM runs
                WHERE created_at < now() - (:days || ' days')::interval
                  AND status NOT IN ('pending', 'running')
                """
            ),
            {"days": retention_days},
        )

        await session.commit()

    counts = {
        "observation_history_deleted": history_result.rowcount,
        "tasks_deleted": tasks_result.rowcount,
        "runs_deleted": runs_result.rowcount,
    }
    if any(counts.values()):
        logger.info("retention purge completed", extra=counts)
    return counts
