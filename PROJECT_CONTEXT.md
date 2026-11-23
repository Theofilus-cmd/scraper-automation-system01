# PROJECT_CONTEXT — Scraper Automation System

Handoff document for the `phase3` branch. Describes what is implemented
today. Design intent lives in `docs/design/` (esp. 18 and 19); setup and
usage live in `README.md`.

## 1. Product

Price/stock monitoring SaaS. A user registers (a personal workspace is
created), adds a public product URL as a **source**, runs a scrape manually
or on a schedule, and gets the current observation, an append-only history,
**alerts** on price / stock-status changes, and alert emails. All data is
workspace-scoped (multi-tenant isolation) behind JWT auth.

## 2. Architecture

| Component | Tech | Role |
| --- | --- | --- |
| frontend | Next.js, :3000 | Multi-page UI (see §7) |
| api | FastAPI, :8000 | REST `/api/v1`, `/healthz`, `/readyz` |
| scheduler | Python | Heartbeat (15s), claim due schedules (10s, batch 20), reconciliation (120s, stuck-task timeout 10 min), retention purge (3600s, 90 days) |
| worker-http | Celery, queue `http` | Scrape tasks, persistence, alert detection, alert email |
| postgres + pgbouncer | PG16, PgBouncer :6432 (scram-sha-256) | Data |
| redis | Redis 7 | Celery broker, execution lease, politeness state, auth rate limits |
| mailhog | :8025 UI, SMTP 1025 | Dev email sink |
| mock-store | FastAPI :4000 | Deterministic local target (static; includes failure fixtures such as `widget-missing-price`, a 503 dead-letter product) |
| migrate | Alembic one-shot | `alembic upgrade head` |

Optional compose profiles: `browser` (worker-browser), `workers`
(worker-notifications, worker-exports), `storage` (minio), `monitoring`
(flower, prometheus, grafana). The browser/notifications/exports workers are
**stubs** (ping tasks only).

### Scrape pipeline

1. `POST /sources/{id}/runs` (or scheduler claim) creates a `run` + durable `task`; Celery message goes to `http`.
2. Worker acquires a Redis **execution lease** (token-based, Lua renew/release, TTL 60s). Redis failure is fail-closed (`CoordinationUnavailableError`); lost lease raises `LeaseOwnershipLostError`.
3. **Politeness enforced during fetch**: per-domain semaphore (max 3), rate limit (2s), cooldown (600s after e.g. 503).
4. SSRF-hardened `fetcher.fetch` (only allowed HTTP path). Hosts resolving to private/loopback/link-local are blocked, except hosts in `SSRF_ALLOWED_HOSTS` (default `mock-store`; empty it outside dev). Public hosts are never affected by that list.
5. Adapter `parse` → `normalize` → validate (required: product name, price, currency, …). Failure → task `failed` with `error_reason` / `validation_errors`, nothing written.
6. One transaction: upsert `products`, update `current_observations`, append `observation_history` (immutable trigger) if values changed, insert `alerts` (UNIQUE `dedupe_key`; redelivery-safe).
7. Alert email: claim-then-send over SMTP (`email_status` pending/sending/sent/failed/skipped, max 5 attempts, claim timeout 300s).
8. Transient errors retry (jittered backoff from durable attempt count, `Retry-After` seconds and HTTP-date honoured, max 3 attempts); persistent failure → `dead_letter`.

Note: the worker persists only `raw_records[0]`, so **one source = one product**.

### Adapters (`backend/app/scraping/adapters/`)

- `mock_store` — JSON-LD pages.
- `shopify` — matches path shapes `/products.json`, `/collections/<c>`, `/products/<handle>` (confidence 0.6/0.5; registry floor applies). Fetches the public JSON endpoint, falls back to JSON-LD. Currency: `products.json` has none, so `fetch()` best-effort reads `/meta.json` `currency` (injected as `_currency`), otherwise `USD`. For a multi-product URL only the first product is stored.

## 3. API (`/api/v1`, Bearer JWT except register/login)

- Auth: `POST /auth/register` (201; returns user, workspace, token), `POST /auth/login`, `GET /auth/me`. Register/login are Redis rate limited (10/60s per IP; login also per email; fail-open if Redis down).
- Sources: `POST /sources` (201 new / 200 existing; 422 `UNSUPPORTED_ADAPTER`), `GET /sources`, `GET|PATCH|DELETE /sources/{id}` (PATCH `status` active|paused; DELETE archives), `POST /sources/{id}/unarchive`, `PUT|DELETE /sources/{id}/schedule`, `POST /sources/{id}/runs` (202, `Idempotency-Key`).
- Runs/tasks: `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/tasks`, `GET /tasks` (filter by status, e.g. `dead_letter`).
- Products: `GET /products`, `GET /products/{id}` (with `current_observation`), `GET /products/{id}/history`.
- Alerts: `GET /alerts` (kind `price_changed` | `stock_status_changed`).
- Legacy: `POST /scrapes`, `GET /scrapes/{task_id}`.
- Business data hub: `POST|GET /business-data/sources`, `GET /business-data/sources/{id}`, `POST …/ingest`, `GET …/runs`, `GET …/records`, `GET …/records/{id}/history`.
- Pagination: cursor (`cursor`, `limit`) → `{data, pagination:{next_cursor, has_more}}`. Errors: `{"error":{code,message,details}}`.

## 4. Database (Alembic 0001–0007)

| Rev | Content |
| --- | --- |
| 0001 | Extensions |
| 0002 | sources, products, current_observations |
| 0003 | Source lifecycle (archive/pause), schedules, runs, tasks, observation_history (immutability trigger + retention bypass); partial unique index on normalized_url for non-archived sources |
| 0004 | users, workspaces, workspace_members; source tenancy |
| 0005 | business_data_sources/schedules/runs/records/record_history |
| 0006 | `shopify` allowed in `sources.adapter_type` |
| 0007 | alerts (+ email delivery columns, UNIQUE dedupe_key) |

Tables: alembic_version, alerts, business_data_*, current_observations, observation_history, products, runs, schedules, sources, tasks, users, workspace_members, workspaces.

## 5. Configuration (env, see `backend/app/core/config.py`)

`JWT_SECRET_KEY` (required, HS256, 30 min), `DATABASE_URL`, `REDIS_URL`, `CORS_ALLOW_ORIGINS`, `SSRF_ALLOWED_HOSTS`, `TASK_EXECUTION_LEASE_SECONDS=60`, `DOMAIN_MAX_CONCURRENCY=3`, `DOMAIN_RATE_LIMIT_SECONDS=2`, `DOMAIN_COOLDOWN_SECONDS=600`, `ALERTS_EMAIL_ENABLED`, `SMTP_HOST/PORT/USERNAME/PASSWORD/USE_STARTTLS`, `NOTIFICATION_FROM_EMAIL`, `AUTH_RATE_LIMIT_*`, `SCRAPER_USER_AGENT` (override in real deployments).

## 6. Run / test

`make bootstrap` (once) → `make up` → `make health`. Tests: `make test-backend` (pytest, real Postgres/Redis), `make test-frontend`, `make lint`, `make typecheck`. Rebuild `api worker-http scheduler` images before testing so tests in the container match the working tree.

## 7. Frontend (`frontend/src/app`)

`/` login/register, summary tiles (alerts, dead-lettered tasks), sources list + add; `/sources/[id]` schedule, manual run, runs, recent alerts, price/stock history charts; `/runs/[id]`; `/alerts`; `/dead-letter`.

## 8. Verified end to end (module F)

Against a live stack with a freshly registered user and real network:
`https://www.allbirds.com/products.json?limit=20` → source created (adapter `shopify`) → manual run succeeded → product (`sku:AR0007U001`, price 7.50 USD, in_stock) appears in `/products`, `/products/{id}`, `/products/{id}/history` and the frontend. Price change simulated by setting the stored price to 10.00 and re-running: history grew 1→2 rows, an alert `price_changed 10.00 → 7.50` was recorded with `email_status=sent`, and the email arrived in MailHog.
Bug found and fixed in this pass: Shopify `products.json` has no currency, so every scrape failed `missing required field: currency`.
Note: `https://shop.polymer-project.org` now returns 404 and is not usable; some Shopify stores answer 429/403 to automated clients.

## 9. Known limitations / not done

- One product per source (first record of a multi-product feed).
- worker-browser / worker-notifications / worker-exports are stubs; no JS-rendered scraping.
- No billing/quotas, webhooks, exports, audit log, or production deployment hardening.
- Email verification is not implemented (`is_verified` stays false).
- Lease renewal during long fetches and Retry-After-based cooldown are covered by earlier phase-3 work; see `docs/design/19-phase-3-reliability-and-politeness-plan.md` for remaining intent.
