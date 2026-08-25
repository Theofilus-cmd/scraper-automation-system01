# 18 — Phase 2 Design Review: Source Lifecycle, Scheduling, Scrape Runs, and Immutable Observation History

Status: **REVISED — direction approved; this revision incorporates 10 required amendments from that review.** Still design-review-only: no code, migrations, patches, or ZIP files have been produced for this document or its predecessor. Once approved, § 10 is the ordered implementation plan.

**Base:** the user's local Phase 1, accepted and tagged `phase1-accepted-2026-08-25` / commit `653bc6f`, with real Docker verification reported: clean rebuild + migration `0002`, `pytest -m integration` 15 passed, full `pytest` suite 83 passed, and a live `POST /api/v1/scrapes` mock-store smoke test.

**Base-commit note, unchanged from the prior revision, stated plainly:** `653bc6f` does not exist in this session's local git history (`git cat-file -e 653bc6f` → "Not a valid object name") — expected, since this round's commit was produced entirely on the user's own machine. This review is grounded against this session's own local HEAD, `13c87b2`, read directly. That is an inference, not a verification: this session's local test tree has exactly 15 integration-marked tests (mechanically counted, not recalled), an exact match to the user's reported "15 passed," which corroborates but does not prove tree equivalence. Re-confirm against the real `653bc6f` tree before any future patch series.

### Revision note (this round)

The prior revision closed with six explicitly-open decisions and asked for approval. All six were decided by the review that approved this document's direction, along with four additional, more specific requirements. All ten are incorporated below, in place, not as an appendix:

1. Observation history append condition — **finalized**, no change from the prior proposal (§ 3.3).
2. Overlapping runs per source — **reversed**: now prohibited, with an atomic DB-level design (§ 4.4) and a dedicated parallelism test (§ 8.4).
3. `POST /api/v1/scrapes` / `GET /api/v1/scrapes/{task_id}` — **reversed**: kept, not replaced, reimplemented as compatibility aliases over the new pipeline (§ 6.6).
4. Archive reversibility — **reversed**: archive is now reversible via an explicit, narrow unarchive operation (§ 3.1, § 6.1).
5. Schedule interval bounds — **finalized**: 15 minutes minimum, 7 days maximum, enforced at both the API and the DB (§ 2.2, § 6.1).
6. Retention — **finalized**: in scope for this phase, including a resolved design for how the immutable-history trigger and the purge job coexist (§ 7.5).
7. **New:** an explicit, feasible deployment guard against running these unauthenticated endpoints as a public/production deployment (§ 7.3).
8. **New:** manual and scheduled triggers enforce one identical invariant; retries never count as a second run (§ 3.2, § 4.4, § 5.4).
9. **Reconfirmed, not weakened:** SSRF validation stays at both creation time and every fetch/redirect hop (§ 7.1).
10. Every section below — schema, lifecycle diagrams, claiming design, endpoint contract, error codes, test matrix, acceptance checklist, and commit plan — is updated to reflect all of the above; nothing is left partially revised.

---

## 0. What this review covers, and what it deliberately doesn't

Doc 15's original roadmap put "Accounts" at Phase 2 and "Scheduling engine" at Phase 3. What's requested here is doc 15's Phase 3 content, arriving under the Phase 2 label, with no accounts. That's stated once, prominently, here, rather than left implicit: every reference to "Phase 2" below means *this* Phase 2, not doc 15's original one. § 1 works through the concrete conflicts this creates against docs 05–08 and how each is resolved. § 9 restates non-goals, including several that fell out of design work, not just the four originally given.

---

## 1. Reuse of existing decisions, and conflicts with them

### 1.1 What's reused directly, unchanged

- **Scheduler poll design (doc 04 §4.1):** `SELECT ... FROM schedules WHERE next_run_at <= now() AND is_active FOR UPDATE SKIP LOCKED`, extended in this revision (§ 4.1) to also exclude sources with an in-flight run — the extension composes with, rather than replaces, the original mechanism.
- **Run/task state machines (doc 07 §1–§2)**, reused in full except the quota-related transition (no billing, § 9).
- **Retry/backoff defaults (doc 07 §3)** via Celery's own `autoretry_for`/`retry_backoff`/`retry_backoff_max`/`retry_jitter` (§ 5.2) — the reasoning doc 04 §4.1 already gives for choosing Celery in the first place.
- **Idempotency design (doc 07 §4):** `idempotency_key = hash(run_id, source_id)`, plus the reconciliation sweep (doc 07 §5), which this revision leans on again for the no-overlap invariant's crash-recovery story (§ 4.4).
- **Error envelope, request-ID middleware, `ApiError`** (doc 06 §1, as already implemented) — unchanged; new codes extend the same uppercased vocabulary.
- **Cursor pagination shape** (doc 06 §1) — unchanged.
- **SSRF hardening** (doc 03 §3, as already implemented) — unchanged and reconfirmed, not weakened (§ 7.1, amendment 9).
- **doc 05 §1/§12 conventions** — uuid pks, `created_at`/`updated_at` + trigger, `CHECK` over native enums, purpose-built (often partial) indexes.
- **doc 12's test-layer taxonomy**, extended with the parallelism layer (§ 8.4).

### 1.2 Conflicts, and how this review resolves each

| # | Doc | What it says | What Phase 2 (as scoped) does instead | Resolution |
|---|---|---|---|---|
| C1 | doc 15 | Phase 2 = Accounts; Phase 3 = Scheduling engine | This document is (part of) doc 15's original Phase 3, under the "Phase 2" label, with no accounts | Documented once, prominently (§ 0). Doc 15 is not edited — it stays the record of the originally-approved sequencing. An accounts phase still must land before any public/multi-tenant deployment (§ 7.3). |
| C2 | doc 05 §4–§8 | Every table carries `workspace_id` (mostly `project_id`) | New tables carry neither | Treated as an additive future migration, not retrofitted now. Table shapes otherwise track doc 05's as closely as the missing tenancy columns allow. |
| C3 | doc 05 §4 | `targets` is the schedulable resource | Phase 1 has `sources`, not `targets` | `sources` becomes the schedulable resource directly (§ 2.1); no new `targets` table. |
| C4 | doc 07 §1 | `tasks.target_id` | `tasks.source_id` | Renamed throughout, matching C3. |
| C5 | doc 05 §6 | `records`/`record_versions` | `current_observations` (existing) / `observation_history` (new) | Renamed to match Phase 1's existing nouns. |
| C6 | doc 06 §3/§6 | `/free/scrapes` (anonymous, 48h) vs. `/projects/{id}/runs` (tenant-scoped) | Phase 2 needs a third shape: durable and schedulable, but tenant-free | New unprefixed `/api/v1/sources`, `/api/v1/runs` (§ 6). |
| C7 | doc 17 (Phase 1, implemented) | `POST /api/v1/scrapes` waits synchronously up to ~35s | **Kept, unchanged in its external contract** — reimplemented as a compatibility alias over the new `runs`/`tasks` pipeline | § 6.6. This reverses the prior revision's proposal to replace it; that proposal is not carried forward. |
| C8 | doc 05 §6 | `record_versions` written on change-or-first-sighting only | Reused, now finalized (not left open) | § 3.3. |
| C9 | doc 04 §3 | Three-layer rate limiting (quota, workspace fairness, domain politeness) | Inapplicable (no tenants) or nothing real to exercise (one cooperative local target) | § 7.2 substitutes a narrower Phase-2 mechanism; layer 3 stays deferred, same reasoning doc 17 Scope Decision 5 already used for robots.txt. |
| C10 | doc 08 §2 | `detect()` always falls back to generic; `adapter_type` re-verified periodically | Phase 1 already deviates; Phase 2 doesn't re-open it | § 9. |

---

## 2. Database additions

### 2.1 `sources` — extended, not replaced

| Column | Type | Notes |
|---|---|---|
| `status` | `text not null default 'active'` | `CHECK (status IN ('active','paused','archived'))`. State machine in § 3.1. |

`url`/`normalized_url`/`adapter_type` stay immutable after creation (no endpoint changes them; re-pointing a source means archiving and creating a new one, § 6.1).

Index: `CREATE INDEX ix_sources_status_active ON sources (id) WHERE status = 'active';`

### 2.2 `schedules` — new, 1:1 with `sources`

```sql
CREATE TABLE schedules (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id        uuid NOT NULL UNIQUE REFERENCES sources(id) ON DELETE RESTRICT,
    interval_minutes integer NOT NULL CHECK (interval_minutes BETWEEN 15 AND 10080),
    is_active        boolean NOT NULL DEFAULT true,
    next_run_at      timestamptz NOT NULL,
    last_run_at      timestamptz NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_schedules_next_run_at_active ON schedules (next_run_at) WHERE is_active;
```

**Interval bounds, finalized:** 15 minutes minimum, 7 days (10080 minutes) maximum — fixed application constants, not per-deployment configuration, enforced at two layers: the API validates before ever issuing a write (`422`, § 6.1), and the `CHECK` constraint above is the DB-level backstop, consistent with this codebase's existing layered-validation pattern (SSRF's create-time check plus fetch-time enforcement, § 7.1, is the same shape). `cron_expression` stays out of scope (§ 9, unchanged).

`source_id UNIQUE` (one schedule per source) is unchanged from the prior revision — still simpler than doc 05 §4's many-schedules-per-target shape, and still additive to widen later.

### 2.3 `runs` and `tasks` — adapted from doc 05 §5, now with the no-overlap constraint

```sql
CREATE TABLE runs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id      uuid NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    schedule_id    uuid NULL REFERENCES schedules(id) ON DELETE SET NULL,
    status         text NOT NULL CHECK (status IN
                     ('pending','running','completed','completed_with_errors','failed','cancelled')),
    triggered_by   text NOT NULL CHECK (triggered_by IN ('schedule','manual')),
    client_idempotency_key text NULL UNIQUE,
    total_tasks    integer NOT NULL DEFAULT 0,
    succeeded_tasks integer NOT NULL DEFAULT 0,
    failed_tasks   integer NOT NULL DEFAULT 0,
    started_at     timestamptz NULL,
    finished_at    timestamptz NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_runs_source_created ON runs (source_id, created_at DESC);
CREATE INDEX ix_runs_in_flight ON runs (id) WHERE status IN ('pending','running');

-- Amendment 2/8: the actual, DB-enforced "at most one in-flight run per
-- source" guarantee. Race-free by construction -- see § 4.4 for the full
-- design and why this specific mechanism (a partial unique index) was
-- chosen over an application-level check alone.
CREATE UNIQUE INDEX uq_runs_one_in_flight_per_source
    ON runs (source_id) WHERE status IN ('pending','running');

CREATE TABLE tasks (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id           uuid NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    source_id        uuid NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    status           text NOT NULL CHECK (status IN
                       ('queued','in_progress','succeeded','failed','retrying','dead_letter')),
    idempotency_key  text NOT NULL UNIQUE,
    attempt_count    integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL DEFAULT 3,
    error_reason     text NULL,
    error_detail     jsonb NULL,
    queued_at        timestamptz NULL,
    started_at       timestamptz NULL,
    finished_at      timestamptz NULL
);
CREATE INDEX ix_tasks_run ON tasks (run_id);
CREATE INDEX ix_tasks_stuck ON tasks (id) WHERE status IN ('queued','retrying');
```

`triggered_by` stays `{'schedule','manual'}` — the legacy compatibility endpoints (§ 6.6) are a manual trigger under the hood and record `triggered_by='manual'` like any other manual run; they are not a third value, since nothing downstream needs to distinguish "manual via the new endpoint" from "manual via the legacy alias."

**Cardinality, unchanged from the prior revision:** every run has exactly one task (`_scrape_source_url()` only consumes `raw_records[0]`, § 9). `runs`/`tasks` stay two tables for the same three reasons as before (retry state at the right granularity, task-scoped idempotency, forward compatibility with listing-page fan-out). `completed_with_errors`/`cancelled` remain in the `CHECK` constraint, unreachable today — flagged, not silently dead.

### 2.4 `observation_history` — adapted from doc 05 §6 `record_versions`

```sql
CREATE TABLE observation_history (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id         uuid NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    run_id             uuid NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    task_id            uuid NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    product_name text NOT NULL, brand text, category text,
    price numeric(12,2) NOT NULL, currency char(3) NOT NULL,
    original_price numeric(12,2), discount numeric(6,2), variant text,
    stock_status text NOT NULL, rating numeric(3,2), review_count integer,
    description text, image_url text,
    is_valid boolean NOT NULL, validation_errors jsonb, scraped_at timestamptz NOT NULL,
    change_summary     jsonb NULL,
    version_created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_observation_history_product_time
    ON observation_history (product_id, version_created_at DESC);
```

`run_id`/`task_id` stay `NOT NULL` (§ 2.5 explains why no backfill row ever needs them nullable).

**Immutability trigger — revised this round to resolve amendment 6's flagged contradiction** (a retention purge, § 7.5, must delete old rows from a table whose whole point is that nothing deletes rows from it):

```sql
CREATE OR REPLACE FUNCTION reject_observation_history_mutation() RETURNS trigger AS $$
BEGIN
    -- The ONLY sanctioned mutation of this table, ever: a DELETE, and only
    -- when the caller has explicitly, narrowly opted into the retention
    -- purge path for this transaction. UPDATE is unconditionally forbidden,
    -- full stop -- there is no legitimate reason to ever edit a history row.
    IF TG_OP = 'DELETE' AND current_setting('app.allow_history_purge', true) = 'true' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'observation_history is append-only: % on id=% is not permitted', TG_OP, OLD.id;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_observation_history_immutable
BEFORE UPDATE OR DELETE ON observation_history
FOR EACH ROW EXECUTE FUNCTION reject_observation_history_mutation();
```

`current_setting('app.allow_history_purge', true)` reads a session-local (`SET LOCAL`) flag that only the retention purge job ever sets, scoped to its own transaction (`SET LOCAL` reverts automatically at transaction end, so it can never leak into an unrelated later query on a pooled connection). This was chosen over a second, more-privileged DB role — the alternative the requirement explicitly allowed for — because Phase 2, like Phase 1, connects through PgBouncer as a single `scraper` role (doc 13 §1); provisioning and managing a second role purely to exempt one job from one trigger is real operational complexity this system doesn't otherwise carry, where a narrowly-scoped session flag achieves the identical safety property (only an explicitly-flagged transaction can ever delete a row; every ordinary read/write connection is still unconditionally blocked) with zero new infrastructure. Full purge design, and why this bypass is safe given the FK topology in § 2.6, is § 7.5.

### 2.5 Migration & backfill from the live Phase 1 database

Unchanged from the prior revision in substance — restated briefly for completeness, since § 2.2's `CHECK` and § 2.3's unique index are new since that version:

1. `sources.status` — added `NOT NULL DEFAULT 'active'`; every pre-existing row backfills correctly by construction (Phase 1 had no pause/archive concept, so every existing row was, in effect, always active).
2. `schedules` — created empty; no backfill (Phase 1 never had schedules; "no row" is the correct unscheduled state).
3. `runs`/`tasks` — created empty, **explicitly not backfilled** from Phase 1's Celery/Redis result-backend history (TTL'd, never durable, nothing real to migrate).
4. `observation_history` — created empty, **also explicitly not backfilled with a synthetic first-sighting row.** `current_observations` already holds each existing product's authoritative last-known state, carried forward unmodified; § 3.3's diff logic compares each new scrape against what's *currently* in `current_observations`, not against "does a history row already exist" — so the first real post-migration scrape of a pre-existing source is diffed correctly with zero synthetic rows and zero special-casing. (A synthetic backfill was considered and rejected in the prior revision for exactly this reason; unchanged here.)
5. Both new triggers land in this same migration.

`downgrade()` drops in strict child-before-parent order: `observation_history` → `tasks` → `runs` → `schedules` → `sources.status`/its index/check — the same order the retention purge itself must respect (§ 7.5), for the same FK reason (§ 2.6).

### 2.6 Full FK/constraint summary

| Table.column / constraint | References | On delete | Why |
|---|---|---|---|
| `schedules.source_id` | `sources.id` | RESTRICT | Archive the source instead of deleting it |
| `runs.source_id` | `sources.id` | RESTRICT | Preserve run history past archival |
| `runs.schedule_id` | `schedules.id` | SET NULL | Deleting a schedule must not erase the runs it produced |
| `tasks.run_id` | `runs.id` | RESTRICT | A task is part of its run's identity |
| `observation_history.product_id` | `products.id` | RESTRICT | History is exactly the data this design must not lose |
| `observation_history.run_id` / `.task_id` | `runs.id` / `tasks.id` | RESTRICT | Same; also why both stay `NOT NULL` |
| `uq_runs_one_in_flight_per_source` (partial unique) | — | — | The no-overlap invariant's actual enforcement mechanism, § 4.4 |
| `schedules.interval_minutes` (`CHECK`) | — | — | 15 min–7 day bound, § 2.2 |

No `CASCADE` appears anywhere — still deliberate, still conservative. It pays off twice over in this revision: it's what makes the no-overlap invariant's transaction-rollback story safe (§ 4.4), and it's what makes the retention purge's child-before-parent ordering *enforced by Postgres itself*, not just by careful application code — an attempt to delete a `runs` row while un-purged `tasks`/`observation_history` still reference it is physically rejected by the same `RESTRICT` constraints already chosen for data-preservation reasons, before the purge job's own ordering logic even needs to be trusted (§ 7.5).

---

## 3. Lifecycle, state transitions, and invariants

### 3.1 Source lifecycle

```
                    ┌───────────────────────────┐
                    │      active ↔ paused       │  reversible, either
                    │   (PATCH /sources/{id})    │  direction, any time
                    └─────────────┬───────────────┘
                                  │
                     archive (DELETE)  │  unarchive (POST .../unarchive)
                                  │      lands on `active`, always
                                  ▼
                             archived
```

- `active ↔ paused`: reversible, via `PATCH /api/v1/sources/{id}`.
- `active|paused → archived`: via `DELETE /api/v1/sources/{id}`. Cascades, in the same transaction, to `schedules.is_active := false` for that source (invariant I2, unchanged).
- **`archived → active` (new this revision, amendment 4):** via `POST /api/v1/sources/{id}/unarchive`, a deliberate, distinct endpoint — not folded into `PATCH`, so un-archiving is never a side effect of a more generic call. Always lands on `active` (not directly on `paused`); if `paused` is wanted afterward, that's a separate, already-existing `PATCH` call. This was chosen over allowing a direct `archived → paused` edge because it's simpler and there's no real scenario that needs the two operations collapsed into one.
- **What unarchiving touches, stated precisely, because "must not lose links" and "don't auto-create a schedule" both turn on this:** unarchive is a single, narrow operation — it flips `sources.status: archived → active` and *nothing else*. It does not touch `products`, `current_observations`, `runs`, `tasks`, or `observation_history` at all, in either direction, because archiving never touched them either (archiving was always a pure status flip plus the schedule-deactivation cascade — nothing was ever deleted or detached, so there is nothing to "restore"; every FK in § 2.6 stays intact through the whole active→archived→active round trip by construction). It does not touch `schedules` either: whatever `is_active` value the schedule had at the moment of archiving (forced `false` by invariant I2) is exactly what it still has after unarchiving. Reactivating a schedule that existed before archiving is an explicit, separate `PUT /sources/{id}/schedule` call (§ 6.1) — never automatic. A source that had no schedule before archiving still has none after unarchiving, trivially, since nothing about unarchive creates one.
- **Invariant I1 (revised this round):** a source can be *manually* triggered (new endpoint or legacy alias) whenever `status != 'archived'` — `paused` no longer blocks a manual trigger, only `archived` does. This is a deliberate refinement over the prior revision's stricter "manual requires `active`" rule: `paused` specifically means "stop auto-firing on schedule," not "this source is untouchable," and a deliberate manual action should still be allowed while paused. `archived` still blocks every trigger path, scheduled or manual, unconditionally. **Scheduled** firing keeps the original, stricter rule: `sources.status = 'active'` *and* `schedules.is_active = true`, both required (§ 4.1).
- **Invariant I2 (unchanged):** archiving a source deactivates its schedule in the same transaction; a schedule can never be `is_active=true` while its source isn't `active`. Enforced in the archive code path, tested directly (§ 8.2).
- **Invariant I3 (unchanged):** `url`/`normalized_url`/`adapter_type` are immutable post-creation.

### 3.2 Scrape-run lifecycle

| From | Event | To |
|---|---|---|
| *(none)* | Scheduler claims a due, unblocked schedule, or a manual/legacy trigger creates a new run | `pending` |
| `pending` | Source confirmed not-archived (manual) or active-with-active-schedule (scheduled), task created | `running` |
| `pending` | Source is archived at claim/trigger time (race) | `failed`, reason `source_archived` |
| `running` | Its one task succeeds | `completed` |
| `running` | Its one task fails or dead-letters | `failed` |

`completed_with_errors`/`cancelled` stay unreachable (§ 2.3).

**Invariant I5 (new this round, amendments 2 and 8): at most one `runs` row with `status IN ('pending','running')` per `source_id`, at all times, enforced at the database level — never merely checked in application code.** This is the single most important addition in this revision; its full mechanism is § 4.4. Two consequences worth stating here, at the lifecycle level rather than only the mechanism level:

- **A retry is not a second run.** A task's retry (§ 5) re-attempts the *same* `tasks.id` — `attempt_count` increments on that one row; the parent `runs.id` stays `running` throughout every attempt, only reaching a terminal status once the task itself does. Invariant I5 is never in tension with retries, because retries never create a new `runs` row to begin with — this falls directly out of keeping `runs` and `tasks` as separate tables (§ 2.3), which turns out to make this requirement trivially, structurally true rather than something that needs a special case.
- **The invariant is identical for every trigger path.** Scheduled claims, the new `POST /sources/{id}/runs`, and the legacy `POST /api/v1/scrapes` alias (§ 6.6) all ultimately attempt the same `INSERT INTO runs`, guarded by the same partial unique index — there is no code path that can create a second in-flight run for a source, regardless of which endpoint or process attempts it. What legitimately differs between the new and legacy endpoints is only the HTTP-level response when that attempt collides with an already-in-flight run (§ 4.4, § 6.6) — never whether the collision is prevented.

### 3.3 Observation-history lifecycle and invariants (amendment 1 — finalized)

- **Append-only**, enforced by the trigger (§ 2.4), now with one narrowly-scoped, explicit exception for the retention purge (§ 7.5) — not a weakening of "immutable," a precisely-defined controlled path for it.
- **Append condition, final:** a row is appended if and only if (a) the triggering scrape was valid, **and** (b) either no prior `current_observations` row existed for this product, or at least one tracked field differs from the prior row. This is exactly the prior revision's proposal (reused from doc 05 §6, C8) — the review approving this document's direction confirmed it without changes, so it is no longer flagged as an open question.
- **Diff computation:** unchanged from the prior revision — `SELECT ... FOR UPDATE` the existing row before upserting (within the same transaction), compute `change_summary = {field: {"from": ..., "to": ...}}` for every differing field, `NULL` on first sighting. The `FOR UPDATE` also correctly serializes two genuinely-concurrent writers to the same product identity, exercised directly by § 8.4's parallelism tests.
- **Invariant I4 (unchanged):** immediately after any valid scrape, `current_observations` for a product and the most recent `observation_history` row for that product agree on every shared field.
- **Accepted limitation (unchanged):** nothing here detects a bad-but-technically-valid observation; anomaly detection stays out of scope (§ 9).

---

## 4. Safe atomic scheduler claiming, including the no-overlap invariant

### 4.1 The claim, revised to exclude sources with an in-flight run

```sql
BEGIN;

SELECT s.id, s.source_id, s.interval_minutes
FROM schedules s
JOIN sources src ON src.id = s.source_id
WHERE s.next_run_at <= now()
  AND s.is_active
  AND src.status = 'active'
  -- Amendment 2: cheap, common-case filter -- skip a source that already
  -- has an in-flight run. This is the fast path; the partial unique index
  -- on runs (§ 2.3, § 4.4) is what makes this airtight under a genuine
  -- race, not this WHERE clause alone.
  AND NOT EXISTS (
      SELECT 1 FROM runs r
      WHERE r.source_id = s.source_id AND r.status IN ('pending', 'running')
  )
ORDER BY s.next_run_at
FOR UPDATE OF s SKIP LOCKED
LIMIT :scheduler_claim_batch_size;

-- for each row claimed above, in the SAME transaction:
UPDATE schedules
SET next_run_at = now() + (interval_minutes || ' minutes')::interval,
    last_run_at = now()
WHERE id = :schedule_id;

INSERT INTO runs (source_id, schedule_id, status, triggered_by)
VALUES (:source_id, :schedule_id, 'pending', 'schedule');
-- If this INSERT violates uq_runs_one_in_flight_per_source (a race the
-- WHERE clause above narrowed but did not fully close -- see § 4.4), the
-- whole transaction rolls back, INCLUDING the next_run_at advance above.
-- The schedule stays due and is reconsidered on the very next poll cycle.
-- This is deliberate, not a bug: it means a source that's genuinely still
-- blocked keeps being reconsidered rather than silently skipping a firing.

COMMIT;

-- AFTER commit, outside the DB transaction, for each newly-created run:
--   create its one `tasks` row (status='queued') and celery_app.send_task(...)
```

### 4.2 Why this is safe under N concurrent scheduler instances

Unchanged from the prior revision: `FOR UPDATE OF s SKIP LOCKED` means a due schedule row locked by one instance's still-open transaction is silently skipped (not blocked, not errored) by any other instance's concurrent claim — the same schedule is never claimed twice in one cycle. `next_run_at` advances inside the claiming transaction, before commit, so the row stops being due the instant the claim commits, independent of what happens to the Celery dispatch afterward. The gap between commit and a successful `.delay()` call is closed by doc 07 §5's existing reconciliation sweep, unchanged.

### 4.3 Multiple scheduler replicas as a tested deliverable

Unchanged: § 8.4 proves this with two concurrent claim iterations against one database; § 10's commit plan makes scheduler replica count a real compose-level knob (defaulting to 1).

### 4.4 The no-overlap invariant: atomic design (amendments 2 and 8, superseding the prior revision's open question)

The prior revision left "allow overlapping runs" vs. "guard against them" as an open decision, defaulting to allow. That default is not carried forward — overlapping runs per source are now prohibited, unconditionally, everywhere.

**The mechanism is the partial unique index from § 2.3:**

```sql
CREATE UNIQUE INDEX uq_runs_one_in_flight_per_source
    ON runs (source_id) WHERE status IN ('pending','running');
```

This is the *actual* safety guarantee — a database-level constraint that Postgres itself enforces on every `INSERT`, regardless of which process or code path attempts it, and regardless of race timing. It is deliberately not "an application-level check, done carefully" — a `SELECT`-then-`INSERT` check in application code alone has an unavoidable TOCTOU gap between the two statements unless the whole thing is itself serialized by something at the database level; the partial unique index removes the gap entirely by making the second, colliding `INSERT` fail outright rather than relying on the check having run recently enough.

**Layered with a cheap pre-check, for a clean caller experience — not for correctness:**

- The scheduler's claim query (§ 4.1) filters out blocked sources in its `WHERE` clause before ever attempting the `INSERT`, so the common case (a source that's obviously still busy) never reaches the constraint at all.
- The manual endpoint (`POST /sources/{id}/runs`, § 6.2) does a `SELECT` for an existing in-flight run first, to return a clean `409 SOURCE_RUN_IN_PROGRESS` with a helpful body in the common case — then still attempts the `INSERT` guarded by the same unique index, and catches a unique-violation on that `INSERT` as the authoritative fallback for the narrow race the pre-check can't close by itself (two concurrent manual requests, or a manual request racing a scheduler claim, for the same source at the same instant). The response is the same `409` either way; the caller never observes which of the two layers caught it.
- This is the same two-layer shape (cheap fast-path check + authoritative DB-level constraint as the real backstop) already used for SSRF at source-create time (§ 7.1) — a consistent pattern across this design, not a one-off.

**Interaction with idempotency-key replay (§ 6.2):** a replayed `POST` carrying an `Idempotency-Key` that matches an existing `runs.client_idempotency_key` is *not* creating a new run — it returns the original run directly, before the in-flight-run check is even consulted. The no-overlap invariant only ever applies to attempts to create a genuinely new run.

**What "no overlap" does *not* need to solve, now that the constraint exists:** the prior revision's § 4.4 spent real effort on whether a short schedule interval could make overlap likely in practice. That question is now moot — overlap is impossible by construction, at any interval, so there is nothing left to reason about there. What replaces it is a narrower, honest question: how often will a schedule find itself *skipped* because its source is still busy? With the new 15-minute minimum interval (§ 2.2) against a worst-case single task lifetime of roughly 4.5 minutes (3 attempts × up to ~30s fetch timeout + up to 60s backoff each), a skip-and-retry-next-cycle is expected to be rare — but it is explicitly allowed to happen, and is the correct, safe behavior when it does (§ 4.1's comment), not a failure mode to be engineered away further.

---

## 5. Retry classification, backoff, jitter, and `next_run_at` interaction

Unchanged in substance from the prior revision; restated briefly, with § 5.4 reinforcing amendment 8.

### 5.1 Classifying error reasons

| `error_reason` | Retryable? |
|---|---|
| `missing_required_field`, `unsupported_adapter`, `parse_error`, `dns_or_ssrf_blocked`, `source_archived` (new, § 3.2) | No — deterministic, retrying can't change the outcome |
| `timeout`, `network_error` (already covers 5xx/429/503, confirmed against `fetcher.py` directly) | Yes — transient |

### 5.2 Celery's own retry machinery

`autoretry_for=(TransientScrapeError,), retry_backoff=True, retry_backoff_max=60, retry_jitter=True, max_retries=2` (2 retries + the first attempt = `max_attempts=3`, doc 07 §3). Permanent-reason failures still return their `{"status": "failed", ...}` dict directly, no exception, no retry — unchanged from today's actual Phase 1 behavior for those reasons.

### 5.3 `Retry-After` handling

Unchanged: `fetcher.py` gains an optional `retry_after` field on `FetchError` for 429/503 responses (currently discarded, confirmed by reading the code); the task's retry path calls `self.retry(countdown=retry_after)` when present, falling back to Celery's own backoff otherwise. Full domain-cooldown token-bucket infrastructure (doc 04 §3 layer 3) stays deferred (§ 9), same reasoning as before.

### 5.4 Retries and the no-overlap invariant (amendment 8, reinforced)

Stated once already at the lifecycle level (§ 3.2) and repeated here because it's the retry system's own responsibility to keep true: every retry of `scrape_source_url` re-invokes the same Celery task instance for the same `tasks.id`, incrementing `attempt_count` on that row. Nothing in the retry path ever creates a new `runs` row, calls the run-creation code, or touches `uq_runs_one_in_flight_per_source` — a task mid-retry keeps its parent run in `running` for the whole retry sequence, and invariant I5 (§ 3.2) is never at risk from retries by construction, not by a check that has to remember to exclude them.

Dead-lettering (exhausting `max_attempts`) still logs at `WARNING` and still does **not** auto-pause the source or schedule — unchanged reasoning (anomaly detection is out of scope, § 9).

---

## 6. API endpoints, payloads, pagination, error codes, idempotency

All endpoints sit under `/api/v1`, unauthenticated (§ 7.3), using the existing error envelope and cursor-pagination shape.

### 6.1 Sources

| Method & path | Behavior |
|---|---|
| `POST /sources` | Body `{"url": "..."}`. Runs § 7.1's create-time checks. Idempotent on `normalized_url`: an existing non-archived match → `200` with that source; no match, or the only match is `archived` → `201` with a new row. `422 UNSUPPORTED_ADAPTER` / `422 DNS_OR_SSRF_BLOCKED` on rejection. |
| `GET /sources` | Cursor-paginated; `?status=` filter. |
| `GET /sources/{id}` | Source, with nested `schedule` (`null` if none) and `current_product` summary (`null` if never scraped). `404 SOURCE_NOT_FOUND`. |
| `PATCH /sources/{id}` | Body `{"status": "active"\|"paused"}` only. `409 SOURCE_ARCHIVED` if archived — use unarchive first. |
| `DELETE /sources/{id}` | Archives (§ 3.1). Cascades to schedule deactivation in the same transaction. `204`, idempotent. |
| `POST /sources/{id}/unarchive` **(new, amendment 4)** | Requires current `status='archived'` (else `409 SOURCE_NOT_ARCHIVED`). Sets `status='active'`. Touches nothing else — no schedule side effect, by design (§ 3.1). `200` with the updated source. |
| `PUT /sources/{id}/schedule` | Upsert. Body `{"interval_minutes": int, "is_active": bool = true}`. `422 SCHEDULE_INTERVAL_TOO_SHORT` (< 15) / `422 SCHEDULE_INTERVAL_TOO_LONG` (> 10080). `409 SOURCE_ARCHIVED` if not active-or-paused. Does not trigger an immediate run (a separate, deliberate action, § 6.2). This is also the explicit reactivation path after an unarchive that left a schedule inactive (§ 3.1) — call it again with `is_active: true`. |
| `DELETE /sources/{id}/schedule` | Removes the schedule entirely. `204`, idempotent. |

### 6.2 Runs

| Method & path | Behavior |
|---|---|
| `POST /sources/{id}/runs` | Trigger a manual run now. Optional `Idempotency-Key` header (§ 4.4). `202` immediately with `{"run_id", "task_id", "status": "pending"}`. `409 SOURCE_ARCHIVED` if archived. **`409 SOURCE_RUN_IN_PROGRESS` (new, amendment 2)** if the source already has a pending/running run — body includes the existing `run_id` so the caller can poll it directly instead. |
| `GET /runs` | Cursor-paginated; `?source_id=&status=&triggered_by=`. |
| `GET /runs/{id}` | `{id, source_id, schedule_id, status, triggered_by, total_tasks, succeeded_tasks, failed_tasks, started_at, finished_at}`. `404 RUN_NOT_FOUND`. |
| `GET /runs/{id}/tasks` | Cursor-paginated (≤1 item today). |

### 6.3 Products & history

Unchanged from the prior revision: `GET /products` (lower priority, § 10), `GET /products/{id}`, `GET /products/{id}/history` (cursor-paginated `observation_history`, newest first).

### 6.4 Error codes (extending the existing vocabulary)

`SOURCE_NOT_FOUND` (404), `SOURCE_ARCHIVED` (409), `SOURCE_NOT_ARCHIVED` (409, new — unarchiving a non-archived source), `SOURCE_RUN_IN_PROGRESS` (409, new), `SCHEDULE_NOT_FOUND` (404), `SCHEDULE_INTERVAL_TOO_SHORT` (422), `SCHEDULE_INTERVAL_TOO_LONG` (422, new), `RUN_NOT_FOUND` (404), `PRODUCT_NOT_FOUND` (404), `DNS_OR_SSRF_BLOCKED` (422 at create-time; the fetch-time occurrence stays a 502 under `FETCH_FAILED`, unchanged — different HTTP status because the caller-facing meaning differs).

### 6.5 Pagination

Unchanged — doc 06 §1's cursor convention on every list endpoint.

### 6.6 `POST /api/v1/scrapes` and `GET /api/v1/scrapes/{task_id}` — kept as compatibility aliases (amendment 3, replaces the prior revision's C7 proposal in full)

**These are not removed, not deprecated-and-scheduled-for-removal on any timeline, and not left on the old, pre-Phase-2 code path.** They stay mounted, and are reimplemented so that every observable request/response behavior a Phase 1 caller already depends on is preserved exactly, while all the actual work routes through the new, durable `runs`/`tasks` pipeline underneath.

**`POST /api/v1/scrapes` — reimplemented:**

1. Body `{"source_url": str}`, unchanged.
2. `registry.detect(source_url)` — `None` → `422 UNSUPPORTED_ADAPTER`, unchanged, identical to today.
3. **Find-or-create the `sources` row** for this URL using exactly § 6.1's `POST /sources` semantics (idempotent on `normalized_url`; a match that's only `archived` gets a fresh row rather than being silently resurrected). This is new — Phase 1's version never had a `sources`-lifecycle concept to consider — but it's invisible to the caller: the request/response shape doesn't change.
4. **Trigger a run for that source, transparently reusing an in-flight one instead of erroring.** This is the one deliberate behavioral difference from the new `POST /sources/{id}/runs` endpoint, and it's introduced precisely *because* of the backward-compatibility requirement, not despite it: an old, unmodified caller has no code path for a `409`, so instead of surfacing `SOURCE_RUN_IN_PROGRESS`, this alias attaches to whatever run is already in-flight for the source (if any) and proceeds to poll *that* one. The underlying invariant (at most one in-flight run per source, § 4.4) is identical and still fully, atomically enforced — only the HTTP-level experience of hitting it differs between the two entry points, exactly as § 3.2 states.
5. **Poll to a terminal `tasks` status** — same `0.5s` interval, same `35.0s` total budget as today, but polling `tasks.status` in Postgres directly instead of `AsyncResult.ready()` over Redis. This is a genuine internal simplification worth noting, not just a swap: it also retires the Phase 1 `_task_exists` hack that reached into Celery's Redis result-backend's undocumented internals (its own comment says as much) — with a durable `tasks` table, "does this id exist" is a plain, indexed `SELECT`.
6. **Response shapes, reproduced exactly:**
   - Terminal success → `200 {"task_id", "status": "completed", "source": {...}, "product": {...}, "observation": {...}, "created": bool}` — assembled from the run's one task's `source_id`/`product_id` and the resulting `current_observations` row, in the same field shapes `_jsonify_observation()` already produces today.
   - Terminal permanent failure (`missing_required_field`/`unsupported_adapter`/`parse_error`) → the same `422` envelope with the same code, via the same `ERROR_REASON_TO_RESPONSE` lookup, unchanged.
   - **Terminal `dead_letter`** (a status Phase 1 never had, since it never retried) → reported using the *last attempt's* `error_reason`, which — because only `timeout`/`network_error` are ever retryable (§ 5.1) — is always one of the two reasons that already map to `502 FETCH_FAILED` today. A caller written against Phase 1 literally cannot distinguish "failed once, immediately, on a transient error" (old behavior, if retries hadn't existed) from "failed after exhausting retries" (new behavior) by response shape — only by latency. No new mapping entry is needed for this; it falls out of § 5.1's classification for free.
   - Not yet terminal after 35s → `202 {"status": "pending", "task_id"}`, unchanged. This fires somewhat more often than in Phase 1, now that a transient failure can take up to ~4.5 minutes to exhaust its retries (§ 4.4) — which does not violate the endpoint's original contract, since "may need to fall back to polling" was always the documented behavior for a slow task, not a guarantee bounded to Phase 1's old, retry-free timing.
   - `task_id` in every response above is `tasks.id` (the new durable Postgres uuid), not a raw Celery task id. This is a deliberate, considered choice, not an oversight: doc 17's actual documented contract only ever promised an opaque id usable with `GET /api/v1/scrapes/{task_id}` — never that the value is a real Celery id usable directly against Celery/Flower. Both are UUID4-shaped strings with no observable format difference to a caller that only round-trips the value, which is the only behavior the documented contract ever covered.
7. **Deprecation signaling, added but non-disruptive:** every response from both legacy endpoints carries a `Deprecation: true` header (and, once a concrete migration path exists, a `Link` header pointing at the § 6.1/§ 6.2 replacement) — informational only, never a behavior change, so existing callers are unaffected but real usage becomes observable (also logged at `INFO`) ahead of any future decision about the endpoints' long-term fate. No removal timeline is set here; that's a future decision, not this one.

**`GET /api/v1/scrapes/{task_id}` — reimplemented:**

- Looks up `tasks.id = :task_id` directly (plain `SELECT`, replacing the Redis-internals hack). Not found → `404 TASK_NOT_FOUND`, unchanged code, now a genuinely reliable check rather than a documented-as-undocumented workaround.
- Not yet terminal → `{"status": "pending", "task_id"}`, unchanged.
- Terminal → the same response construction as step 6 above.
- **New-vocabulary statuses (`retrying`, `queued`, `in_progress`) are never surfaced to this endpoint's caller** — they all report as `{"status": "pending", ...}`, exactly matching the only two states (pending or terminal) a Phase-1-vintage caller has ever seen or coded against.

---

## 7. Security delta

### 7.1 SSRF — reconfirmed, not weakened (amendment 9)

Unchanged from the prior revision, restated because the requirement asked for explicit reconfirmation rather than assuming silence means agreement: `fetcher.py`'s existing scheme-allowlist / `ssrf_allowed_hosts`-bypass / resolved-IP-private-range check, re-validated on every redirect hop, remains the sole, unmodified security boundary for actual fetches. The create-time check proposed for `POST /sources` (§ 6.1) reuses the same validation logic for a synchronous fast-fail at creation, but is explicitly a UX/data-quality improvement layered on top, not a replacement — DNS can still change between create-time and fetch-time (DNS rebinding), which is exactly why the fetch-time, per-redirect-hop check stays authoritative and untouched. Nothing in this revision alters `fetcher.py`'s existing behavior.

### 7.2 Rate/concurrency limits

Unchanged from the prior revision: Celery worker concurrency already provides backpressure; `SCHEDULER_CLAIM_BATCH_SIZE` bounds per-cycle claim volume. One addition worth noting: the no-overlap invariant (§ 4.4) is itself an incidental concurrency control — it caps in-flight scrape activity *per source* at exactly one, everywhere, which doc 04 §3's original three-layer design never quite gave for free. Full domain-politeness token-bucket infrastructure stays deferred (§ 9).

### 7.3 Authorization assumptions, with a real deployment guard (amendment 7)

**Stated explicitly, as required:** every endpoint in § 6, old and new, is unauthenticated. There is no accounts system to authenticate against (C1). Anyone who can reach the API can create, schedule, pause, archive, or unarchive any source. This is acceptable only for a local-development/single-operator deployment and **must not** be exposed publicly before real, workspace-scoped auth lands (doc 15's originally-sequenced Accounts phase, C1).

**A real, feasible, application-level guard, added this revision:** `Settings` already carries an `environment` field (`development` default, confirmed by reading `app/core/config.py` directly). `create_app()` gains a startup check: if `environment == "production"`, mounting the `sources`/`runs`/`products` routers additionally requires a second, deliberately-named setting — `unauthenticated_source_api_ack: bool = False` (env var `UNAUTHENTICATED_SOURCE_API_ACK`) — to be explicitly set `true`; if it isn't, the app refuses to start rather than silently serving these routes. This is feasible within Phase 2's scope (a startup-time `if`, no new infrastructure) and is a real guard, not a documentation-only gesture: a production-flagged deployment cannot accidentally expose these endpoints without a deliberate, separately-named acknowledgment.

**Stated honestly, because a partial guard oversold as complete would be worse than no guard:** this cannot detect or prevent an operator running a `development`-flagged instance on a publicly-reachable network interface, or manually overriding the ack — `uvicorn`'s bind address is a process argument in `docker-compose.yml`, not something visible to `create_app()` from inside the ASGI app, and no application-level check can substitute for actual network/deployment policy. The guard's real scope is: it prevents this from being exposed *by omission* under the `production` label this system already uses to distinguish deployment intent; it is not a network firewall, and isn't presented as one.

### 7.4 Safe error persistence

Unchanged from the prior revision: `tasks.error_detail` stays populated only from `FetchError`'s reason/message and `ValidationResult.errors` — never raw fetched HTML or full response bodies. Raw-artifact-on-failure storage (doc 03 §5) stays out of scope (§ 9).

### 7.5 Data retention, with the immutability-trigger interaction resolved (amendment 6, fully in scope this round)

**Setting:** `history_retention_days: int = 90` (env `HISTORY_RETENTION_DAYS`), applied uniformly to `observation_history`, `tasks`, and `runs` — one window, matching the single setting requested, rather than separate windows per table.

**What's never purged, stated as a hard constraint:** `current_observations`, `sources`, and `products` are never touched by retention, under any configuration — these are current-state tables, not history (doc 07 §7's own reasoning, reused directly: "records — current state — [are] untouched by retention since [they're] not history").

**The trigger/purge interaction, resolved (this was the prior revision's genuinely unresolved contradiction — a trigger that unconditionally rejects every `DELETE` cannot coexist with a purge job that must `DELETE` old rows, and it was wrong to leave that unaddressed):** § 2.4's revised trigger permits a `DELETE` on `observation_history` only inside a transaction that has explicitly set `app.allow_history_purge = 'true'` via `SET LOCAL` — a flag only the purge job's own code ever sets, scoped to exactly its own transaction. Every other connection, including the application's normal read/write paths, is still unconditionally blocked from ever mutating this table. `UPDATE` is blocked unconditionally, with no bypass, for anyone, always — retention only ever needs `DELETE`.

**Purge job, run from the scheduler process (an hourly-cadence check within its existing loop, distinct from its ~10s schedule-claiming poll — a `DELETE` sweep doesn't need to run every poll tick):**

```sql
BEGIN;
SET LOCAL app.allow_history_purge = 'true';

-- Child before parent, required by the RESTRICT FKs in § 2.6 -- Postgres
-- itself rejects deleting a runs/tasks row that un-purged history still
-- references, so this ordering isn't just documented, it's enforced.

DELETE FROM observation_history
WHERE version_created_at < now() - (:history_retention_days || ' days')::interval
  -- never purge history belonging to a still-in-flight run, regardless of
  -- its own timestamp (defense-in-depth; in practice unreachable, since an
  -- in-flight run's history rows are always recent -- kept anyway).
  AND run_id NOT IN (SELECT id FROM runs WHERE status IN ('pending', 'running'));

DELETE FROM tasks
WHERE run_id IN (
    SELECT id FROM runs
    WHERE created_at < now() - (:history_retention_days || ' days')::interval
      AND status NOT IN ('pending', 'running')
);

DELETE FROM runs
WHERE created_at < now() - (:history_retention_days || ' days')::interval
  AND status NOT IN ('pending', 'running');
  -- No explicit "and no tasks/history still reference this run" check is
  -- needed here beyond what already ran above: if either prior DELETE
  -- left something behind (it won't, in the normal path), the RESTRICT
  -- FK makes this statement fail loudly rather than silently orphaning
  -- anything -- the same safety net § 2.6 already relies on elsewhere.

COMMIT;
```

**Why a trigger-level bypass over the alternative the requirement also allowed (a separate trusted DB role):** stated already at § 2.4 — a session-scoped flag achieves the same controlled-path guarantee without a second role, stays consistent with this system's existing single-role architecture, and keeps the exemption visible in one piece of application code rather than implicit in which connection happens to be used.

---

## 8. Test matrix and acceptance checklist

### 8.1 Unit (pure, no DB/network)

Unchanged from the prior revision (source status transitions, retry classification, backoff/jitter bounds, history diff computation, idempotency-key derivation) plus:

- Schedule interval bounds: 14/15/10080/10081 minutes as explicit boundary cases (reject/accept/accept/reject).
- Legacy `dead_letter` → response-shape mapping: confirm it resolves to the same `502 FETCH_FAILED` shape as a first-attempt `timeout`/`network_error` would (§ 6.6), table-driven over both reasons.

### 8.2 Integration (real Postgres/Redis, `@pytest.mark.integration`)

Unchanged items from the prior revision (migration backfill, immutability-trigger rejection, first/repeat/changed-field scrape history behavior, invalid-write-preserves-snapshot, scheduler claim correctness, `Retry-After` honoring, manual-run idempotency-key replay) plus:

- **Unarchive round-trip:** archive a source with an active schedule, products, observations, and history → unarchive → assert every one of those rows and FKs is untouched (byte-identical `products`/`current_observations`/`observation_history` content, schedule row still present but still `is_active=false`) — directly proving amendment 4's "must not lose links" requirement, not just asserting it by design argument.
- **Schedule reactivation is explicit:** after the unarchive above, confirm the schedule does *not* start firing again until an explicit `PUT .../schedule` call.
- **No-overlap, single-process:** attempt to `INSERT` a second `pending`/`running` run for a source that already has one → the unique-violation is raised by Postgres, confirming the constraint itself, independent of any application-level pre-check.
- **Retention purge, end-to-end:** seed `observation_history`/`tasks`/`runs` rows both inside and outside the retention window (including one belonging to a still-`running` run) → run the purge → assert only the eligible, terminal, past-window rows are gone, in the correct child-before-parent order, and that `current_observations`/`sources`/`products` are completely untouched.
- **Purge bypass is narrowly scoped:** immediately after a purge transaction commits, attempt a direct `DELETE` against `observation_history` from a fresh, ordinary transaction (no `SET LOCAL` flag) → still rejected — proving the bypass doesn't leak past its own transaction.
- **Legacy `POST /api/v1/scrapes` attaches instead of erroring:** trigger a run via the new `POST /sources/{id}/runs`, then immediately call legacy `POST /api/v1/scrapes` for the same URL → asserts it returns/polls the *same* `run`'s task rather than creating a second one or surfacing a `409`.
- **Deployment guard:** start the app with `environment=production` and no ack → refuses to start (or refuses to mount the routers, per final implementation choice); with the ack set → starts normally.

### 8.3 API (TestClient, `@pytest.mark.integration` where writes need real Postgres)

Unchanged items (source CRUD, schedule upsert/delete, manual trigger → poll, history pagination, one test per error code) plus:

- Full lifecycle round trip through the API layer: create → archive → unarchive → patch to paused → patch to active, asserting the correct status/body at each step including the `409`s for invalid transitions (`PATCH` while archived, `unarchive` while not archived).
- `POST /sources/{id}/runs` against a source with an in-flight run → `409 SOURCE_RUN_IN_PROGRESS`, body includes the existing `run_id`.
- Legacy-endpoint response-shape tests: byte-for-byte comparison of success/failure/pending body shapes against the shapes documented in § 6.6, for every outcome category including a dead-lettered task.
- `Deprecation` header present on every legacy-endpoint response, absent on new-endpoint responses.

### 8.4 Parallelism — a distinct layer (extended significantly this revision)

- **Concurrent scheduler instances, no double-fire** (unchanged from the prior revision): two claim iterations concurrently against N due, unblocked schedules → exactly N runs, never `2N`.
- **No-overlap invariant under real concurrency (new, amendment 2's explicit ask for "an atomic ... design and parallelism test proving it"):** fire several concurrent `POST /sources/{id}/runs` requests for the *same* source with no idempotency key → exactly one succeeds (`202`, a new run), every other one receives `409 SOURCE_RUN_IN_PROGRESS` referencing that same run — asserted against real concurrent requests (e.g. `asyncio.gather` of several client calls), not a serialized simulation of concurrency.
- **Scheduler claim racing a manual trigger for the same source:** a schedule becomes due for source X at the same moment a manual `POST /sources/{id}/runs` fires for X → exactly one run is created between the two, the other observes the collision (the scheduler skips and retries next cycle per § 4.1's comment; the manual caller gets `409`) — proving the invariant holds *across* trigger types, not just within one.
- **Concurrent writes to the same product identity** (unchanged from the prior revision, now explicitly tied to the `FOR UPDATE`-serialized diff logic, § 3.3): asserts no corrupted row and a coherent, if not caller-predictable, final state — the accepted last-write-wins limitation is asserted explicitly, not assumed.
- **Task redelivery / idempotent claim** (unchanged, doc 07 §4): a task claimed by one worker is safely a no-op for a second.

### 8.5 Acceptance checklist (for the future implementation PR)

- [ ] Migration applies cleanly against the live, accepted Phase 1 database; `sources.status` backfills correctly; nothing pre-existing is altered.
- [ ] `observation_history` rejects direct `UPDATE`/`DELETE` from an ordinary transaction, in a live Postgres; the purge job's flagged `DELETE` succeeds in the same live Postgres; the bypass is confirmed not to leak past its own transaction.
- [ ] Two scheduler instances never double-fire a schedule, demonstrated against a live, concurrent run.
- [ ] The no-overlap invariant holds under real concurrent load from every trigger path (scheduler, new manual endpoint, legacy alias), demonstrated, not just asserted by design argument.
- [ ] A transient-failure task retries to `max_attempts`, honors `Retry-After`, dead-letters on exhaustion, with real elapsed-time evidence.
- [ ] `POST /api/v1/scrapes` / `GET /api/v1/scrapes/{task_id}` produce response bodies indistinguishable from Phase 1's originally-shipped shapes, for every outcome category, verified against real requests.
- [ ] Unarchive round-trip loses nothing (§ 8.2), and schedule reactivation after unarchive is confirmed explicit, not automatic.
- [ ] Retention purge removes only eligible rows, in the correct order, never touches `current_observations`/`sources`/`products`, demonstrated against real seeded data spanning the retention boundary.
- [ ] The production deployment guard actually blocks an unmounted/unstarted app when unacknowledged, and allows normal startup when acknowledged.
- [ ] `ruff check .` / `mypy app` (strict) clean on every new/changed file.
- [ ] `docker compose down -v && docker compose up -d --build` + full `pytest` + `pytest -m integration`, all real, all green, on a machine with real network access — this review makes no claim about what will happen when this is actually run (see this document's own Base-commit note).

---

## 9. Non-goals (Phase 2, as scoped here)

Unchanged from the prior revision, restated for completeness, with one addition:

- No multi-tenancy, no auth (§ 7.3) — every endpoint stays open, guarded only by the deployment-mode check.
- No real websites beyond mock-store; no domain-politeness token-bucket infrastructure.
- No browser automation.
- No alerts, billing, or anomaly/diff detection.
- No cron-expression or timezone-aware schedules — interval-only, now with fixed 15-minute/7-day bounds (§ 2.2).
- No multi-product-per-source fan-out execution (schema stays ready; `_scrape_source_url()`'s behavior doesn't change).
- No adapter re-detection over time.
- No run cancellation.
- No raw-HTML-on-failure artifact storage.
- No standalone `GET /api/v1/tasks/{id}`.
- **New:** no direct `archived → paused` transition (unarchive always lands on `active`; reach `paused` via the existing, separate `active → paused` step, § 3.1).

---

## 10. Commit plan (proposed sequence — not implemented)

Revised to reflect the amendments; still small, ordered, each independently buildable/testable:

1. **Migration `0003`** — every schema addition in § 2, including the revised immutability trigger with its purge bypass, the `uq_runs_one_in_flight_per_source` partial unique index, and the `interval_minutes` `CHECK`. No application code changes yet.
2. **ORM models + repository-layer functions** — `Schedule`/`Run`/`Task`/`ObservationHistory` models; source-lifecycle transitions including unarchive (§ 3.1); the diff-and-append upsert (§ 3.3); run-creation helpers that surface a clean collision result for both the new and legacy callers to translate differently (§ 4.4, § 6.6).
3. **Retry rework** — `TransientScrapeError`, Celery retry decoration, `Retry-After` capture (§ 5).
4. **Scheduler** — real claim-and-dispatch poll loop (§ 4.1) replacing the heartbeat stub; the hourly retention-purge check (§ 7.5) added to the same process.
5. **API** — new `app/api/v1/sources.py` (including `/unarchive`), `app/api/v1/runs.py`, `app/api/v1/products.py`; **`app/api/v1/scrapes.py` reimplemented in place as the compatibility alias (§ 6.6), not removed.**
6. **Security delta** — shared SSRF check reused at create-time (§ 7.1, unchanged in behavior); the production deployment guard in `create_app()` (§ 7.3).
7. **Tests** — the full § 8 matrix, including the expanded parallelism layer (§ 8.4) and the legacy-compatibility tests (§ 8.2/§ 8.3).
8. **Docs** — this document (18) stands alongside 05–08; doc 00's document map gains a row for it once this phase is actually implemented, per doc 00 §6's rule, not as part of this review.

Retention (previously listed as optional/stretch) is now a required part of step 4, not deferred, per amendment 6.

---

## Decisions finalized this round

Every item the prior revision left open has been resolved by the amendments incorporated above — recorded here, in doc 00 §4's own style, so the trail is explicit:

1. History append condition — ✅ on first valid sighting or actual change, no row otherwise (§ 3.3).
2. Overlapping runs — ✅ prohibited, atomically, at the database level (§ 4.4).
3. Legacy endpoint fate — ✅ kept, reimplemented as a compatibility alias, response shapes preserved exactly (§ 6.6).
4. Archive reversibility — ✅ reversible via an explicit unarchive operation; nothing is ever lost (§ 3.1).
5. Interval bounds — ✅ 15 minutes minimum, 7 days maximum, enforced at both API and DB layers (§ 2.2).
6. Retention scope and the trigger contradiction — ✅ in scope this phase; resolved via a session-scoped, narrowly-audited trigger bypass (§ 7.5).

Plus the four amendments that were new this round, all incorporated: a real, honestly-scoped deployment guard (§ 7.3); the explicit manual/scheduler invariant equivalence with retries excluded (§ 3.2, § 5.4); SSRF reconfirmed unweakened (§ 7.1); and this document fully updated end-to-end rather than patched in place.
