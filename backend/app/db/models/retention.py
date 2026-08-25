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
function remembering the right order (doc 18 §2.6). All three deletes stay
inside the one `async with get_session()` transaction below and commit
together -- a real Postgres/asyncpg error on any one of them (including
the bind-type bug this file used to have) aborts the whole purge for this
cycle rather than leaving it partially applied; the next scheduled run
retries the same, still-eligible rows.

Every DELETE keeps excluding rows tied to a still-in-flight run
(`status IN ('pending', 'running')`) -- doc 18 §7.5's "never purge history
belonging to a still-in-flight run" -- unconditionally, regardless of how
old `version_created_at`/`created_at` is. That protection is untouched by
either fix below.

Acceptance-review fix #1 (bind type): `now() - (:days || ' days')::interval`
concatenates a bind parameter into a string before casting -- asyncpg
resolves that placeholder's expected wire type as `text` from the `||`
operator, and a plain Python `int` is not a conversion asyncpg will
perform on your behalf (`DataError: invalid input for query argument $1:
90 (expected str, got int)`). All three DELETEs shared this identical
pattern (asyncpg's error surfaces on whichever one runs first in a given
transaction, but all three were equally broken). Fixed by using Postgres's
own `make_interval(days => ...)` instead: its `days` parameter is declared
as a plain `int` in Postgres itself, an exact, unambiguous match for the
`int` this function already receives -- no string, and no implicit
numeric-promotion reasoning, ever enters the picture.

Acceptance-review fix #2 (typing): `AsyncSession.execute()` is typed to
return the general `Result[Any]`, which has no `.rowcount` -- only its
`CursorResult` subclass (what a DML statement actually returns at
runtime) declares that attribute. This is a confirmed, open SQLAlchemy
typing limitation (upstream: sqlalchemy/sqlalchemy#12913 and #9185 --
"add overloads to Session.execute() to... qualify... we are returning
CursorResult" is still open), not something a stricter local annotation
can route around: `Result` and `CursorResult` are genuinely different
static types, and SQLAlchemy's own maintainers point to `cast()`, not a
broader ignore, as the fix. `_execute_delete()` below does exactly that,
once, so none of the three call sites need their own cast or ignore.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_session

logger = get_logger(__name__)


async def _execute_delete(session: AsyncSession, sql: str, retention_days: int) -> int:
    """Runs one retention DELETE and returns its row count as a precisely
    typed `int` -- see this module's docstring, fix #2, for why the
    `cast` here is a targeted, documented narrowing rather than a blanket
    suppression: it only asserts the one attribute access
    (`AsyncSession.execute()` on a DML `text()` statement really does
    return a `CursorResult` at runtime; mypy just can't derive that
    statically today), and would not hide an unrelated type error
    introduced later in this function.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(text(sql), {"retention_days": retention_days}),
    )
    # int(...), not a bare `return result.rowcount`: guarantees this
    # function's declared `-> int` holds regardless of exactly how
    # precisely `.rowcount` itself resolves under a given mypy/stub
    # combination -- a real conversion, not a suppression, and a no-op at
    # runtime (`rowcount` is already a plain int from the DBAPI cursor).
    return int(result.rowcount)


async def purge_expired_history(*, retention_days: int) -> dict[str, int]:
    async with get_session() as session:
        await session.execute(text("SET LOCAL app.allow_history_purge = 'true'"))

        history_deleted = await _execute_delete(
            session,
            """
            DELETE FROM observation_history
            WHERE version_created_at < now() - make_interval(days => :retention_days)
              AND run_id NOT IN (
                  SELECT id FROM runs WHERE status IN ('pending', 'running')
              )
            """,
            retention_days,
        )
        tasks_deleted = await _execute_delete(
            session,
            """
            DELETE FROM tasks
            WHERE run_id IN (
                SELECT id FROM runs
                WHERE created_at < now() - make_interval(days => :retention_days)
                  AND status NOT IN ('pending', 'running')
            )
            """,
            retention_days,
        )
        runs_deleted = await _execute_delete(
            session,
            """
            DELETE FROM runs
            WHERE created_at < now() - make_interval(days => :retention_days)
              AND status NOT IN ('pending', 'running')
            """,
            retention_days,
        )

        await session.commit()

    counts = {
        "observation_history_deleted": history_deleted,
        "tasks_deleted": tasks_deleted,
        "runs_deleted": runs_deleted,
    }
    if any(counts.values()):
        logger.info("retention purge completed", extra=counts)
    return counts
