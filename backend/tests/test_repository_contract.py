"""Pure unit test, no DB/network -- pins down `upsert_scrape_result()`'s
public contract after the Phase 2 signature change (repository.py's own
module docstring): `source_id`/`run_id`/`task_id`, not Phase 1's
`source_url`/`adapter_slug`. A regression back toward the old contract
(or an accidental rename away from the new one) would be a real, silent
breaking change for every caller -- app/workers/tasks_http.py, and every
integration test in this suite -- this catches it immediately, without
needing a live Postgres, and runs in the fast default `pytest` pass
rather than being gated behind `-m integration` like the tests that
actually exercise this function.

Acceptance-review note: the real Phase 1 compatibility promise (doc 18
§6.6) was always about the `/api/v1/scrapes` HTTP contract, never this
internal function's Python signature -- that promise is covered
end-to-end, over real HTTP, by test_scrapes_integration.py. This function
is implementation plumbing one layer below that promise, and Phase 2
deliberately changed its shape: durable `run_id`/`task_id` provenance on
every `observation_history` row (doc 18 §2.4) cannot be synthesized
without either fabricating a run/task under the hood on every call (which
this codebase does not do -- see repository.py's module docstring) or
making `observation_history.run_id`/`task_id` nullable (migration 0003
does not; doc 18 §2.5 explains why no row ever needs them nullable). A
literal `source_url`/`adapter_slug` compatibility shim was considered and
rejected for exactly that reason: it cannot honor durable provenance
without fabricating it. This test is the "add coverage for the chosen
compatibility/migration behavior" ask -- confirming the new contract
stays what it is, deliberately, going forward.
"""

import inspect

from app.db.models.repository import upsert_scrape_result


def test_upsert_scrape_result_requires_the_phase_2_provenance_contract() -> None:
    params = inspect.signature(upsert_scrape_result).parameters

    assert set(params) == {"source_id", "run_id", "task_id", "record", "validation"}
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())

    # The Phase 1 contract this replaced. If either of these reappears as
    # an accepted keyword, it's either a deliberate, real backward-compat
    # shim (which must still route through real run_id/task_id
    # provenance -- see this file's own module docstring -- not fabricate
    # it) or an accidental regression back toward the retired contract.
    assert "source_url" not in params
    assert "adapter_slug" not in params
