# 04 — System Architecture & Module Boundaries

## 1. Component overview

Your original pipeline sketch (Frontend → API → PostgreSQL → Scheduler → Queue → Workers → Parser/Normalizer/Validator → Records → Diff → Alerts → Export/API) is the right mental model but not a strictly linear data flow — several of these are independent services that all read/write shared state rather than passing data hand-to-hand. Here's the corrected shape:

```
                                   ┌─────────────────────┐
                                   │   Next.js Frontend   │
                                   └──────────┬───────────┘
                                              │ HTTPS/JSON
                                   ┌──────────▼───────────┐
                                   │   FastAPI (API)       │◄──────────┐
                                   │  - auth                │            │ API keys
                                   │  - CRUD (workspaces,    │            │ (paid tiers)
                                   │    projects, targets,   │            │
                                   │    schedules, alerts)   │            │
                                   │  - billing webhooks     │            │
                                   │  - reads runs/records/   │           │
                                   │    diffs for UI          │           │
                                   └──────────┬────────────┘            │
                                              │ SQL (via PgBouncer)      │
                        ┌─────────────────────┼───────────────────────┬─┘
                        │                     │                       │
               ┌────────▼────────┐   ┌────────▼─────────┐   ┌────────▼────────┐
               │   PostgreSQL     │   │      Redis         │   │  S3-compatible   │
               │ (source of truth,│   │ (broker, cache,     │   │  object storage   │
               │  RLS-enforced)   │   │  rate-limit state)  │   │ (exports, failure │
               └────────▲────────┘   └────────▲─────────┘   │  artifacts)        │
                        │                     │              └────────▲────────┘
               ┌────────┴────────┐            │                       │
               │    Scheduler     │            │                       │
               │ (polls `schedules│            │                       │
               │  ` table, creates│            │                       │
               │  due `runs`)     │            │                       │
               └────────┬────────┘            │                       │
                        │ enqueues "expand run"│                       │
                        ▼                     │                       │
               ┌──────────────────┐            │                       │
               │   Coordinator     │            │                       │
               │ (expands a run    │            │                       │
               │  into per-target  │────────────┘                       │
               │  tasks, checks     │  Celery tasks on                  │
               │  plan quota +      │  queue=http / queue=browser /     │
               │  fairness cap)     │  queue=notifications              │
               └──────────────────┘                                    │
                        │                                              │
        ┌───────────────┼────────────────┐                             │
        ▼                                ▼                             │
┌───────────────┐               ┌────────────────┐                     │
│  HTTP workers   │               │ Browser workers  │                     │
│ httpx+selectolax│               │   Playwright      │                     │
│ (Celery, queue= │               │ (Celery, queue=    │                     │
│  http)          │               │  browser)          │                     │
└───────┬───────┘               └────────┬───────┘                     │
        │      both call the SAME adapter → normalize → validate       │
        └───────────────┬───────────────┘                              │
                         ▼                                             │
              ┌────────────────────┐                                   │
              │  Diff engine step   │  (runs inline at end of each      │
              │  (per record write) │   task's write, not a separate    │
              └──────────┬─────────┘   pass — see doc 10)               │
                         ▼                                             │
              ┌────────────────────┐                                   │
              │  Alert/notification │──── Celery, queue=notifications ──┘
              │  worker (email now)  │
              └────────────────────┘

               ┌────────────────────┐
               │  Export worker       │  (CSV/XLSX/JSON → object storage,
               │  (Celery, queue=      │   presigned URL returned via API)
               │   exports)            │
               └────────────────────┘
```

Everything below the API line is **workers**, not request/response HTTP services — they're driven by the queue, not by user requests. The API never scrapes anything itself; it only reads/writes Postgres and enqueues/reads job state.

## 2. Module boundaries

Hard rule carried from your constraints: **the web/API layer and the scraping workers are separate deployables that share only the database, the queue, and object storage** — never in-process calls between them. This is enforced by repo structure, not just convention:

```
backend/
  app/
    api/                 # FastAPI routers + request/response schemas only.
      v1/
        auth.py
        workspaces.py
        projects.py
        targets.py
        schedules.py
        runs.py
        records.py
        diffs.py
        alerts.py
        exports.py
        billing.py
        api_keys.py
        free_scrape.py
    core/                # cross-cutting: config, security, deps, RLS session helpers
    domain/              # pure business logic, framework-agnostic
      quota.py           # plan-limit + fairness-cap checks (called by API AND coordinator)
      diffing.py
      validation.py
    db/
      models/            # SQLAlchemy models
      migrations/        # Alembic
    scraping/
      adapters/
        base.py           # SourceAdapter Protocol (doc 08)
        generic.py
        shopify.py
        woocommerce.py
        registry.py
      fetcher.py           # SSRF-hardened HTTP client (doc 03 §3)
      browser.py            # Playwright session/context management
      normalize.py
    scheduler/              # standalone process, imports domain/ + db/ only
    coordinator/             # standalone process/Celery task, imports domain/ + db/ + scraping/adapters (registry only, not fetch/parse)
    workers/
      tasks_http.py           # Celery tasks, queue=http
      tasks_browser.py         # Celery tasks, queue=browser
      tasks_notifications.py    # Celery tasks, queue=notifications
      tasks_exports.py          # Celery tasks, queue=exports
    billing/
      provider.py                # BillingProvider Protocol
      stripe_provider.py
    notifications/
      provider.py                  # NotificationProvider Protocol
      email_provider.py
      webhook_provider.py          # stubbed, disabled
    alerts/
      rules.py
tests/
  unit/
  integration/
  fixtures/
    adapters/{generic,shopify,woocommerce}/*.html + *.expected.json
frontend/                          # Next.js + TS, separate deployable, talks only to the API over HTTP
```

`api/` never imports from `scraping/` or `workers/` except the adapter **registry** (to validate a target's URL and show detection results at save-time — that's a pure function, no network I/O). `scheduler/`, `coordinator/`, and `workers/` never import from `api/`. Both sides import `domain/` and `db/`, which contain no FastAPI or Celery-specific code — this is what lets quota logic, validation, and diffing be unit-tested without spinning up either the API or a worker.

## 3. Rate limiting — three independent layers

A single "rate limit" concept can't express everything you asked for, so this is three separate mechanisms, checked at different points:

| Layer | Question it answers | Enforced by | Storage |
|---|---|---|---|
| **1. Plan quota** | Has this workspace used its monthly scrape/browser-scrape allowance? | Coordinator, before creating tasks for a run; API, before allowing a manual run/schedule create | Postgres `usage_counters` (durable, billing-accurate) |
| **2. Fairness concurrency cap** | Is this workspace/project already monopolizing the shared worker pool? | Coordinator, before dispatching each task | Redis counter `inflight:{workspace_id}` (fast, ephemeral, self-healing on TTL) |
| **3. Domain politeness cap** | Is this target *domain* already at its concurrency/rate limit right now, and did it just tell us to back off? | Worker, immediately before making the actual HTTP/browser request | Redis token bucket `domain_rl:{domain}` + cooldown flag `domain_cooldown:{domain}` |

Layer 3 detail, since this is the one with real scraping-reliability consequences:

- Default per-domain concurrency: **3 simultaneous requests** (configurable range 2–5, env `DOMAIN_MAX_CONCURRENCY`), enforced via a Redis-backed semaphore (Lua script for atomic acquire/release, avoiding race conditions across worker processes).
- Default per-domain rate: token bucket refilling at a configurable rate (default 1 req/2s sustained per domain, independent of the concurrency cap — concurrency caps *simultaneous* requests, the token bucket caps *sustained rate* even if requests are fast).
- `robots.txt` `Crawl-delay`, when present, raises the floor on the token bucket refill rate for that domain (never lowers it below what robots.txt asks for).
- **HTTP 429/503 is treated as an explicit backpressure signal**, not just a generic retryable error: the worker (a) honors a `Retry-After` header if present as the retry delay, (b) halves that domain's token bucket rate for the next 10 minutes via the cooldown flag, and (c) still counts toward the task's own retry/backoff budget (doc 07 §3). This means one domain misbehaving under load automatically slows *itself* down without needing a human to notice and intervene.
- No pool anywhere in the system is unbounded: worker concurrency, per-domain concurrency, per-workspace in-flight cap, DB pool size, and Redis connection pool size are all explicit, configurable values (doc 00 §3, doc 07 §2).

## 4. Key technology decisions

### 4.1 Queue: Celery + Redis (not RQ, Dramatiq, Arq, or Temporal)

| Option | Why not (for v1) |
|---|---|
| **RQ** | Simpler API, but weaker built-in retry/backoff and no first-class rate limiting; RQ-Scheduler (needed for recurring schedules) is thinly maintained. Would need to hand-roll what Celery gives for free. |
| **Dramatiq** | Clean design, decent retry middleware, but smaller ecosystem/community — harder to find prior art when something breaks in production at 2am. |
| **Arq** | Asyncio-native (nice fit with FastAPI), built-in cron, lightweight — but thinner track record at production scale, sparser monitoring tooling (no Flower equivalent), less mature complex-retry/routing story. Worth revisiting once/if the team is comfortable operating something newer. |
| **Temporal** | Actually the *architecturally* correct tool for durable, retryable, stateful workflows — durable execution is its whole point. Rejected for v1 purely on operational cost: it's a separate server + its own DB, a real learning curve, and this system's job graph (fetch → parse → normalize → validate → write) isn't complex enough yet to justify it. Worth reconsidering if a future feature needs true multi-step durable workflows (e.g., a multi-page product enrichment pipeline). |
| **Celery + Redis** ✅ | Most mature retry/backoff primitives (`autoretry_for`, `retry_backoff`, `retry_backoff_max`, `retry_jitter`), native task routing (clean fit for http/browser/notifications/exports queue separation), huge community/documentation for troubleshooting, doesn't require the API to be async-aware of the workers at all (workers are a fully separate process/deployable, matching the module-boundary requirement in §2). |

**Durability caveat, addressed head-on:** Celery's usual broker is Redis or RabbitMQ. RabbitMQ gives stronger message-durability guarantees out of the box; Redis is primarily in-memory and, even with AOF persistence (`appendonly yes`, `appendfsync everysec`), has a small window where a broker crash could lose a just-enqueued message. Given the MVP scale target (doc 00 §3) and that we already need Redis for caching and rate-limit state, adding RabbitMQ now is more operational surface than the risk justifies. Instead:

- Redis persistence is turned on (AOF, `everysec` fsync — bounded ~1s loss window).
- **Every task write is idempotent** (doc 07 §4), so redelivery or a rare lost message never causes a duplicate or corrupt record.
- A **reconciliation sweep** (a lightweight periodic Celery beat-free job, run every few minutes by the scheduler process) finds tasks stuck in `queued` past a timeout with no corresponding worker activity and re-enqueues them. This closes the durability gap in software rather than in infrastructure, and is cheap insurance regardless of broker choice.
- If/when scale or SLA requirements outgrow this, swapping the broker to RabbitMQ is a config change (Celery abstracts the broker) — documented as part of the scaling path (doc 14 §7), not a v1 requirement.

**Scheduling detail:** recurring schedules are **not** driven by Celery Beat's static schedule. Celery Beat is a single scheduled-task-list process that's awkward for *dynamic, per-tenant, per-target* schedules stored in Postgres and edited via the API. Instead, the `scheduler/` process is a small poller: every N seconds, `SELECT ... FROM schedules WHERE next_run_at <= now() AND is_active FOR UPDATE SKIP LOCKED`, create the `run` row, advance `next_run_at`, enqueue a "coordinate this run" Celery task. `FOR UPDATE SKIP LOCKED` makes this safe to run as more than one instance for HA later without double-firing a schedule. This is standard, boring, and easy to reason about — appropriate for how central "did the schedule actually fire" is to the whole product.

### 4.2 Auth: short-lived JWT + refresh token (cookies) + separate API keys — not plain sessions, not bare JWT-in-localStorage

| Approach | Tradeoff |
|---|---|
| Server-side sessions only | Simple revocation, but a DB/Redis round-trip on every request, and doesn't naturally extend to the "API access" feature paid plans need. |
| JWT in `localStorage` | Stateless and fast, but readable by any injected script — an XSS bug becomes a full account-takeover primitive. Rejected outright for the interactive web app. |
| **JWT (access + refresh) in httpOnly cookies, + separate opaque API keys for programmatic access** ✅ | Access token: short-lived (15 min), stateless verification (no DB hit on most requests), httpOnly+Secure+SameSite=Lax cookie (not readable by JS, so XSS can't steal it directly). Refresh token: longer-lived, rotated on use, stored hashed server-side (so it *can* be revoked — closing JWT's usual revocation weakness), also httpOnly cookie. State-changing requests require a custom header the browser only sends same-origin (CSRF mitigation, on top of `SameSite=Lax`). |

**API keys are a deliberately separate mechanism**, not "a JWT with a long expiry": opaque, prefixed for identification (`sk_live_...`), hashed at rest like a password, scoped per-workspace, individually revocable, with their own rate limit tier. This cleanly serves the paid "API access" feature without stretching the interactive-session auth model to cover machine-to-machine use, and means revoking one leaked key never requires touching anyone's login session.

### 4.3 Billing: abstraction over Stripe, entitlements kept separate

`BillingProvider` protocol (`create_customer`, `create_checkout_session`, `get_subscription`, `cancel_subscription`, `handle_webhook_event`) with `StripeBillingProvider` as the only v1 implementation. Two things this buys us, both explicitly requested:

1. **Provider swappability** — if Paddle/LemonSqueezy/etc. ever makes sense (e.g., merchant-of-record for VAT handling), only `stripe_provider.py` changes.
2. **Entitlements never depend on a live Stripe call.** `plans` and `subscriptions` in Postgres are the source of truth for "what can this workspace do right now" — Stripe webhooks keep them in sync asynchronously. The request path that checks "is this workspace over quota" never calls out to Stripe. Full detail in doc 09.

### 4.4 Extraction stack: httpx + selectolax by default, Playwright only when declared necessary

`selectolax` (Modest/lexbor-based) over BeautifulSoup as the primary parser for the static path — meaningfully faster on the CSS-selector/XPath-style extraction this system does constantly, which matters once we're parsing thousands of pages/day on one VPS. BeautifulSoup remains an acceptable fallback for any page whose HTML is malformed enough that selectolax struggles (documented per-adapter if it comes up, not a default).

Each adapter declares `requires_js: bool` (doc 08 §1). Generic/Shopify/WooCommerce all default to `False` — all three typically render product pages server-side. Playwright is only invoked for adapters/targets explicitly marked as needing it, dispatched to the separate `queue=browser` worker pool so heavy browser-rendering work never starves or gets starved by lightweight HTTP fetches (§1 diagram, doc 00 §3 worker sizing).

### 4.5 PgBouncer in front of Postgres

Added because the naive topology (FastAPI running multiple Uvicorn workers, plus several independent Celery worker processes, all opening their own Postgres connections) risks connection exhaustion well before the VPS's CPU/RAM limits are hit — Postgres connections are expensive relative to how cheap it is to add PgBouncer in transaction-pooling mode. One more container in Compose now is far cheaper than an emergency retrofit under load later.

### 4.6 Redis usage separation

One Redis instance for v1 (matches the "start simple, document the scaling path" scale decision), logically separated by key prefix so each concern is independently inspectable and independently movable to its own instance later without a redesign:

| Prefix | Purpose |
|---|---|
| `celery:*` | Broker + result backend |
| `domain_rl:*`, `domain_cooldown:*` | Per-domain rate limiter state (§3 layer 3) |
| `inflight:*` | Per-workspace fairness counters (§3 layer 2) |
| `cache:*` | Short-TTL read caching (e.g. plan limits, robots.txt per domain) |
| `session:*` | Refresh-token-rotation bookkeeping |

Scaling path: split `celery:*` onto its own Redis instance first (broker traffic is the highest-volume, most latency-sensitive consumer) — noted in doc 14 §7, not needed at MVP scale.

## 5. End-to-end walkthrough (Mode 2, one scheduled run)

1. `scheduler` claims a due row from `schedules`, creates a `runs` row (`status=pending`), advances `next_run_at`, enqueues `coordinate_run(run_id)`.
2. `coordinator` task picks it up: resolves active targets for the project, checks plan quota (layer 1) — if exceeded, run is marked `failed` with reason `quota_exceeded` and an in-app/email notice is queued, nothing is scraped. Otherwise creates one `tasks` row per target (`status=queued`, deterministic `idempotency_key`), and enqueues one Celery message per task onto `queue=http` or `queue=browser` per the target's adapter, checking the fairness cap (layer 2) as it dispatches.
3. A worker picks up a task: checks/sets idempotency marker, checks domain cap (layer 3), fetches via the SSRF-hardened fetcher (or Playwright), runs the target's adapter `parse()`, then the shared `normalize()` and `validate()`.
4. On success: upserts into `records` (current state) keyed by product identity, appends to `record_versions` if anything changed, runs the diff step inline, marks the task `succeeded`.
5. On transient failure (timeout, 5xx, network error, 429/503): retries with backoff+jitter up to the max attempt count, updating the domain cooldown flag on 429/503; exhausting retries → `dead_letter`.
6. On permanent failure (404, `robots_disallowed`, `blocked_by_target`, required-field validation failure): fails immediately, no retry, reason recorded.
7. Once all tasks for the run are terminal, run status is recomputed (`completed` / `completed_with_errors` / `failed`), and if the run wasn't flagged `suspicious` (doc 10 §4), queued diffs are batched into alert evaluation → `queue=notifications`.

Full state machine detail in doc 07; diff/anomaly logic in doc 10.
