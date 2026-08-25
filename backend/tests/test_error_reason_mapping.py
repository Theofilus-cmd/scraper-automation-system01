"""Pure unit tests, no DB/network -- doc 18 §8.1:
- idempotency-key derivation
- retry classification (TASK_TRANSIENT_REASONS / TASK_PERMANENT_REASONS)
  against ERROR_REASON_TO_RESPONSE's completeness
- legacy dead_letter -> response-shape mapping: a dead-lettered task must
  resolve to the exact same (status_code, code) a first-attempt failure of
  the same reason would, table-driven over both transient reasons (doc 18
  §6.6 point 6: a Phase-1 caller cannot tell the two apart by response
  shape, only by latency -- this pins that down as a real equality, not
  just prose)
- schedule interval bound constants (doc 18 §2.2, amendment 5)
"""

import uuid

import pytest

from app.db.models.lifecycle import (
    RUN_STATUSES,
    SCHEDULE_MAX_INTERVAL_MINUTES,
    SCHEDULE_MIN_INTERVAL_MINUTES,
    TASK_PERMANENT_REASONS,
    TASK_STATUSES,
    TASK_TERMINAL_STATUSES,
    TASK_TRANSIENT_REASONS,
)
from app.db.models.runs_repository import _task_idempotency_key
from app.workers.tasks_http import ERROR_REASON_TO_RESPONSE


def test_schedule_interval_bounds_match_doc_18_amendment_5() -> None:
    assert SCHEDULE_MIN_INTERVAL_MINUTES == 15
    assert SCHEDULE_MAX_INTERVAL_MINUTES == 10080  # 7 days


def test_idempotency_key_is_deterministic_and_run_scoped() -> None:
    run_id = uuid.uuid4()
    source_id = uuid.uuid4()

    key_a = _task_idempotency_key(run_id, source_id)
    key_b = _task_idempotency_key(run_id, source_id)

    assert key_a == key_b
    assert str(run_id) in key_a
    assert str(source_id) in key_a
    # A different run for the same source must never collide -- this is
    # exactly what lets tasks.idempotency_key stay UNIQUE across retries
    # of *different* runs for the same source (doc 07 §4).
    assert _task_idempotency_key(uuid.uuid4(), source_id) != key_a


def test_transient_and_permanent_reasons_are_disjoint() -> None:
    assert set(TASK_TRANSIENT_REASONS).isdisjoint(TASK_PERMANENT_REASONS)


def test_error_reason_to_response_covers_every_classified_reason() -> None:
    """Every reason this codebase can ever classify (transient or
    permanent, doc 18 §5.1) has a mapping -- an unmapped reason would
    silently fall through to a generic 500 in both app/api/v1/scrapes.py's
    legacy alias and app/api/v1/error_mapping.py's spirit.
    """
    classified = set(TASK_TRANSIENT_REASONS) | set(TASK_PERMANENT_REASONS)
    assert classified <= set(ERROR_REASON_TO_RESPONSE.keys())


@pytest.mark.parametrize("reason", TASK_TRANSIENT_REASONS)
def test_dead_letter_resolves_identically_to_a_first_attempt_failure(reason: str) -> None:
    """doc 18 §6.6 point 6: a dead-lettered task is reported using its
    last attempt's error_reason via the exact same
    ERROR_REASON_TO_RESPONSE lookup a first-attempt failure of that same
    reason would use -- there is only one entry per reason, so this holds
    by construction, but pin it down as an explicit equality rather than
    leaving it as an unverified claim about the dict's shape.
    """
    assert ERROR_REASON_TO_RESPONSE[reason] == (502, "FETCH_FAILED")


def test_task_terminal_statuses_are_a_strict_subset_of_task_statuses() -> None:
    assert set(TASK_TERMINAL_STATUSES) <= set(TASK_STATUSES)
    non_terminal = set(TASK_STATUSES) - set(TASK_TERMINAL_STATUSES)
    # doc 18 §6.6: exactly these three statuses report as "pending" to a
    # legacy caller -- queued/in_progress/retrying, never a fourth or a
    # missing one.
    assert non_terminal == {"queued", "in_progress", "retrying"}


def test_run_statuses_include_the_two_reachable_terminal_states() -> None:
    # completed_with_errors/cancelled stay listed but unreachable today
    # (doc 18 §2.3) -- this only pins down that the two states this phase
    # actually reaches are present, not that the others are absent.
    assert {"completed", "failed"} <= set(RUN_STATUSES)
