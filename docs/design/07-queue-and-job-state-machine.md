# 07 — Queue & Job State Machine

Terminology, used consistently everywhere: a **run** is one firing of a schedule (or one manual trigger) against a project; a **task** is one target within a run. "Scrapes" (the billing unit, doc 09 §2) = terminal tasks.

## 1. Run state machine

| From | Event | To |
|---|---|---|
| *(none)* | Scheduler claims a due schedule, or a user/API triggers manually | `pending` |
| `pending` | Coordinator confirms quota available, creates ≥1 task | `running` |
| `pending` | Coordinator finds quota exceeded, or zero active targets | `failed` (reason recorded, zero tasks created) |
| `pending` / `running` | User/API cancels | `cancelled` |
| `running` | All tasks terminal, zero failed/dead_letter | `completed` |
| `running` | All tasks terminal, some failed/dead_letter but ≥1 succeeded | `completed_with_errors` |
| `running` | All tasks terminal, zero succeeded | `failed` |

`runs.total_tasks/succeeded_tasks/failed_tasks` are updated transactionally as each task reaches a terminal state (not recomputed by a periodic scan) — this is what makes `GET /runs/{id}` progress-bar-cheap (doc 06 §6).

## 2. Task state machine

| From | Event | To |
|---|---|---|
| *(none)* | Coordinator expands a run | `queued` |
| `queued` | Worker picks it up, passes idempotency check | `in_progress` |
| `queued` | Worker picks it up, idempotency check finds it already handled (redelivery) | *(no-op — see §4)* |
| `in_progress` | Fetch + parse + normalize + validate all succeed | `succeeded` |
| `in_progress` | Permanent failure (`robots_disallowed`, `blocked_by_target`, `missing_required_field`, `unsupported_adapter`, `dns_or_ssrf_blocked`, HTTP 404) | `failed` — **no retry** |
| `in_progress` | Transient failure (`timeout`, `network_error`, HTTP 5xx, 429/503) and `attempt_count < max_attempts` | `retrying` |
| `in_progress` | Transient failure and `attempt_count >= max_attempts` | `dead_letter` |
| `retrying` | Backoff delay elapses, re-enqueued | `queued` |

`failed` and `dead_letter` are both terminal and both count toward `runs.failed_tasks` — they're distinguished for operators (`failed` = we know why and it won't help to retry; `dead_letter` = it might have worked with more attempts, worth a human glance if it's happening a lot on one domain) but identical from the run-status and user-facing "did my scrape work" point of view.

## 3. Retry, backoff, and timeout defaults

All of these are env-configurable (doc 00 §3); values below are the shipped defaults.

| Parameter | Default | Notes |
|---|---|---|
| HTTP fetch timeout | connect 5s / read 15s / total 30s | |
| Browser-render timeout | 45s total | Playwright page load + extraction |
| `max_attempts` | 3 | i.e. up to 2 retries after the first attempt |
| Backoff | `2^attempt * 1s` base, capped at 60s, **+ full jitter** (random 0–100% of the computed delay) | Jitter prevents synchronized retry storms when many tasks fail at once (e.g. a target domain has a brief outage) |
| 429/503 handling | Honor `Retry-After` header if present as the delay; otherwise use standard backoff | Also triggers the domain cooldown described in doc 04 §3 |
| Dead-letter | After `max_attempts` transient failures | Visible in a dedicated ops view; if **all** tasks in a run dead-letter, that's itself an anomaly worth a system alert (doc 11 §5), since it usually means the whole domain is blocking us rather than one bad page |

## 4. Idempotency

Celery's `acks_late=True` (used here so a worker crash mid-task doesn't lose the task) means **at-least-once delivery** — the same task message can, in rare cases, be delivered twice. Two layers make that safe:

1. **Deterministic idempotency key** (`tasks.idempotency_key = hash(run_id, target_id)`), checked via `SETNX`-style claim in Redis with a TTL slightly longer than the task's max possible duration. A worker that can't claim the key assumes another worker is already handling (or just finished) this exact task and no-ops.
2. **Idempotent writes regardless.** Even if two workers somehow both executed the same task, the `records` upsert is keyed on `(target_id, product_identity_key)` — a second identical write is a no-op change, and `record_versions`/`diffs` are only written when the normalized output actually differs from the prior version, so a duplicate execution never produces a duplicate diff or a false "changed twice" alert.

This two-layer approach is why Redis-as-broker's small durability gap (doc 04 §4.1) is an acceptable v1 tradeoff: the failure modes it could cause (a message delivered twice, or a message rarely lost) are exactly the two failure modes this design already has to handle safely for other reasons (worker crashes, network blips).

## 5. Reconciliation sweep

Runs alongside the scheduler's normal polling loop, every 2 minutes by default:

```sql
SELECT id FROM tasks
WHERE status IN ('queued', 'in_progress')
  AND queued_at < now() - interval '10 minutes'
```

Any task found is treated as stuck (broker message lost, or a worker died without releasing/reporting) and is re-enqueued with its existing `idempotency_key` — safe by construction per §4. This is the concrete mechanism, not just an aspiration, behind "a task is never silently dropped" (doc 02 §1 F2.7).

## 6. Coordinator dispatch logic (expanded from doc 04 §5)

```
for target in active_targets(project):
    if not quota_available(workspace):           # layer 1 — doc 04 §3
        run.status = "failed"; run.error = "quota_exceeded"; break
    task = create_task(run, target)               # status=queued
    queue_name = "browser" if adapter(target).requires_js else "http"
    if not fairness_slot_available(workspace):    # layer 2 — doc 04 §3
        delay_dispatch(task)                       # stays queued, retried by reconciliation-style check, not failed
        continue
    celery_app.send_task(queue_name, task_id=task.id, idempotency_key=task.idempotency_key)
```

Domain politeness (layer 3) is deliberately **not** checked here — it's checked by the worker immediately before the real network request, because domain state changes faster than dispatch-time snapshots would stay accurate, and because it needs to run local to wherever the actual HTTP call happens.

## 7. What "job progress and run history" (F2.16) means concretely

- `GET /runs/{id}` is cheap enough (§1) to poll every few seconds from the UI for a live progress bar during an active run.
- `GET /runs/{id}/tasks` gives the per-target breakdown behind that bar — this is where a user sees *which* URLs failed and why, not just an aggregate count (doc 02 §1 F1.4/F2.16, "preserve failed URLs and error reasons" from the original constraints).
- Run history (`GET /projects/{id}/runs`) is retained per the plan's `history_retention_days` (doc 05 §3) — a purge job removes `runs`/`tasks`/`record_versions` rows older than the workspace's current plan window, `records` (current state) is untouched by retention since it's not history.
