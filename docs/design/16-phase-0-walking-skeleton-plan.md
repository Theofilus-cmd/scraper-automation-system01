# 16 — Phase 0: Walking Skeleton — Implementation Plan

Status: **PLAN ONLY — no files generated yet.** Per your instruction, this is A–H for review. Nothing below is written to the repo until you approve it or ask for changes.

Scope discipline, restated so it's checkable against what follows: no Stripe, no real email sending, no Playwright/Chromium, no billing, no domain models (`users`/`workspaces`/etc.), no Phase 1 features. Phase 0 proves the skeleton boots, every service is reachable and healthy, and the plumbing (DB↔API, Redis↔workers, migration gate, CI) is real — not that any feature works yet.

**Revision note (2026-08-24):** revised for hardware-constrained local development — full detail in §D/§F/§G/§H below. Production architecture (doc 14) is unchanged; this is a local Phase 0 development adjustment only.

---

## A/B/C. File-by-file plan, project tree, and purpose

### B. Complete project tree

```
scraper-automation-system/
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Makefile
├── README.md
├── docs/
│   └── design/                       # existing — 17 docs after this one
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── alembic.ini
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── core/
│   │   │   ├── __init__.py
│   │   │   ├── config.py
│   │   │   ├── logging.py
│   │   │   └── health.py
│   │   ├── db/
│   │   │   ├── __init__.py
│   │   │   ├── session.py
│   │   │   └── migrations/
│   │   │       ├── env.py
│   │   │       ├── script.py.mako
│   │   │       └── versions/
│   │   │           └── 0001_enable_extensions.py
│   │   ├── scheduler/
│   │   │   ├── __init__.py
│   │   │   └── main.py
│   │   └── workers/
│   │       ├── __init__.py
│   │       ├── celery_app.py
│   │       ├── tasks_http.py
│   │       ├── tasks_browser.py
│   │       ├── tasks_notifications.py
│   │       └── tasks_exports.py
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       ├── test_health.py
│       └── test_readyz_integration.py
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── package-lock.json
│   ├── tsconfig.json
│   ├── next.config.ts
│   ├── eslint.config.mjs
│   ├── vitest.config.ts
│   ├── src/
│   │   └── app/
│   │       ├── layout.tsx
│   │       ├── page.tsx
│   │       └── globals.css
│   └── tests/
│       └── page.test.tsx
├── tools/
│   └── mock-store/
│       ├── Dockerfile
│       ├── pyproject.toml
│       ├── uv.lock
│       └── app.py
├── scripts/
│   ├── wait_for_healthy.sh
│   └── reset_dev_env.sh
└── .github/
    └── workflows/
        └── ci.yml
```

40 new files (not counting `docs/`). Nothing under `backend/app/api/` or `backend/app/db/models/` yet — those are Phase 1/Phase 2 (doc 15), and creating empty placeholder packages for them now would just be dead scaffolding.

### A/C. Purpose of every file

**Root**

| File | Why |
|---|---|
| `.env.example` | Every variable Phase 0 needs, placeholder/dummy values only, committed. `HTTP_WORKER_CONCURRENCY` now defaults to `2` (down from doc 13's original target of `6`) to match the single-replica local default (§D) — production's `.env` restores the doc 00 §3-approved value. Otherwise superset-ready: includes the rest of the worker-tuning vars from doc 13 §2 even though nothing reads them yet. |
| `.gitignore` | Standard Python/Node ignores + `.env`. Explicitly does **not** ignore `uv.lock` / `package-lock.json` — lockfiles are committed for reproducible builds. |
| `docker-compose.yml` | As specified in doc 13 §1, with the Phase 0 deltas in §D below: some services stubbed, `Dockerfile.playwright` not present yet, and — new in this revision — a minimal default profile with `browser`/`workers`/`storage`/`monitoring` as opt-in Compose profiles so nothing heavy starts unless asked for. |
| `Makefile` | One-word entry points for the commands in §F, including `up-browser`/`up-workers`/`up-storage`/`up-monitoring` for the optional profiles — not new behavior, just names for `docker compose ...` incantations so they're not re-typed/mis-typed. |
| `README.md` | Project one-liner, link to `docs/design/`, prerequisites summary (→ §E), quickstart (→ §F) **leading with the lightweight default profile**, optional profiles documented right after as clearly opt-in, current phase status. The front door for anyone (including future-you) opening the repo cold. |

**`backend/`** — one image, five processes (api/scheduler/worker-http/worker-notifications/worker-exports all run `backend/Dockerfile`, differing only by `command:` — doc 04 §2's module-boundary structure starting from day one, not retrofitted later)

| File | Why |
|---|---|
| `Dockerfile` | Multi-stage: builder installs deps via `uv sync --frozen` into a venv, runtime stage copies the venv + `app/`, runs as a non-root user. Default `CMD` runs `uvicorn`; every other service overrides `command:` in Compose. |
| `pyproject.toml` | Project metadata + deps (`fastapi`, `uvicorn`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `celery`, `redis`, `pydantic-settings`, `python-json-logger`) + `[tool.ruff]`/`[tool.mypy]` config in one file, no separate config files. |
| `uv.lock` | Pinned, reproducible dependency graph — committed. |
| `alembic.ini` | Points at `app/db/migrations`, reads `DATABASE_URL` from env rather than a hardcoded connection string. |
| `app/main.py` | Creates the FastAPI app, adds CORS middleware (`http://localhost:3000` allowed — the frontend needs this to call `/readyz` from the browser), mounts the two health routes from `core/health.py`, wires structured logging on startup. **No business routes.** |
| `app/core/config.py` | `pydantic-settings` `Settings` class reading every env var — the one place config is parsed, everything else imports from here rather than calling `os.environ` directly. |
| `app/core/logging.py` | JSON-structured logging setup (doc 11 §1) — configured once, at process start, for the API, the scheduler, and every worker. |
| `app/core/health.py` | `GET /healthz` (process-up only, no dependency check — doc 11 §3) and `GET /readyz` (real `SELECT 1` through `db/session.py`'s engine + a real Redis `PING`; 503 if either fails). This is genuine, correct behavior, not a hardcoded `return {"status": "ok"}`. |
| `app/db/session.py` | Async SQLAlchemy engine + session factory, reading `DATABASE_URL` (pointed at PgBouncer, doc 04 §4.5) from `core/config.py`. No models module yet — that's Phase 2. |
| `app/db/migrations/env.py` | Alembic environment configured for an **async** engine from the start (the common retrofit pain point if you start sync and switch later) — even though Phase 0 has nothing to autogenerate against. |
| `app/db/migrations/versions/0001_enable_extensions.py` | The one real migration: `CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS citext;`. Not a throwaway — doc 05's conventions (`gen_random_uuid()` PKs, `citext` email) depend on both, so this is genuinely useful groundwork, and it proves the `migrate` service / `alembic upgrade head` gate (doc 04 §4.1, doc 14 §3) end-to-end. |
| `app/scheduler/main.py` | Stub: a loop that, every N seconds, does a real `SELECT 1` and a real Redis `PING`, logs a heartbeat (`scheduler heartbeat: db_ok=true redis_ok=true`), sleeps. Proves the scheduler process boots and reaches both dependencies — no `schedules` table exists yet (Phase 2/3), so there's nothing real to poll. |
| `app/workers/celery_app.py` | Celery app instance, broker/backend = Redis, four named queues (`http`, `browser`, `notifications`, `exports`) matching doc 04 §1 — routing exists for real even though the tasks routed are stubs. |
| `app/workers/tasks_http.py`, `tasks_browser.py`, `tasks_notifications.py`, `tasks_exports.py` | Each defines exactly one task, e.g. `ping()` — logs its queue name + a timestamp-free marker, returns a small dict. Enough to prove: worker boots, registers with the broker, and can execute and complete a task on its queue. Manually triggerable (exact command in §G). |
| `tests/test_health.py` | Unit test (no real DB/Redis needed): `/healthz` always returns 200. |
| `tests/test_readyz_integration.py` | Marked `@pytest.mark.integration`; asserts `/readyz` returns 200 when run against the real Compose stack (DB+Redis reachable) — run inside the `api` container, not on the bare host. |

**`frontend/`**

| File | Why |
|---|---|
| `Dockerfile` | `node:22-alpine`, installs deps, runs `npm run dev` — dev-mode container for now (doc 13 §1); a production build stage is added when this actually ships (doc 14). |
| `package.json` / `package-lock.json` | Next.js (App Router) + TypeScript + Vitest + React Testing Library + ESLint. Lockfile committed. |
| `tsconfig.json`, `eslint.config.mjs` (flat config), `vitest.config.ts` | Standard Next.js TS/lint/test config — nothing custom yet. |
| `src/app/layout.tsx` | Root layout — HTML shell, no design system yet. |
| `src/app/page.tsx` | The home/health page: fetches the API's `/readyz` (via `NEXT_PUBLIC_API_URL`) client-side and renders a plain status badge ("API: reachable" / "API: unreachable") plus a static page title. This is what makes Phase 0 a genuine *walking* skeleton — frontend and backend are proven wired together, not just independently up. |
| `src/app/globals.css` | Minimal reset, no design system — that's a later phase. |
| `tests/page.test.tsx` | One smoke test: the page renders without crashing and shows the expected heading text (API status is mocked, not live, in this test). |

**`tools/mock-store/`**

| File | Why |
|---|---|
| `Dockerfile`, `pyproject.toml`, `uv.lock` | Same pattern as `backend/`, a fully independent tiny project (deliberately not sharing `backend/`'s dependency tree — it's a test fixture, not part of the product). |
| `app.py` | Bare FastAPI app: `/` returns a placeholder HTML page, `/healthz` returns 200. Real Shopify/WooCommerce/generic-shaped fixture pages (doc 12 §7) get built out starting Phase 1, when there's an adapter that needs something to scrape. |

**`scripts/`**

| File | Why |
|---|---|
| `wait_for_healthy.sh` | Polls `docker compose ps --format json` until every service is `running`/`healthy` (or times out) — used by CI's smoke-test job (§D) and available locally instead of eyeballing `docker compose ps`. |
| `reset_dev_env.sh` | `docker compose down -v` with a confirmation prompt — the destructive full-reset path, named so it's not fat-fingered from shell history. |

**`.github/workflows/ci.yml`**

Runs on every push/PR: backend lint (`ruff`) + type check (`mypy`) + unit tests; frontend lint (`eslint`) + type check (`tsc --noEmit`) + `npm run build`; `gitleaks` secret scan on the diff; `pip-audit`/`npm audit` dependency scan; and a **smoke-test job** that actually runs `docker compose up -d --build`, waits via `wait_for_healthy.sh`, curls `/healthz` and `/readyz`, then tears down — so "all containers reach healthy" (your requirement) is verified by CI on every change, not just asserted locally once. Assumes **GitHub Actions** since you haven't specified a CI platform — flag if you're actually on GitLab/other and I'll adjust the one file this affects.

---

## D. Docker services — default profile, optional profiles, and Phase 0 state

Revised for your hardware: plain `docker compose up -d --build` (**no flags**) now starts only **10** lightweight services. Everything else sits behind an opt-in Compose profile and starts only when you ask for it.

| Service | Profile | Starts with plain `up`? | Phase 0 state |
|---|---|---|---|
| `postgres` | *(none — default)* | Yes | Real |
| `pgbouncer` | *(none — default)* | Yes | Real |
| `redis` | *(none — default)* | Yes | Real |
| `mailhog` | *(none — default)* | Yes | Real, unused (nothing sends mail yet) |
| `migrate` | *(none — default)* | Yes | Real — runs `0001_enable_extensions`, exits 0, gates everything else |
| `api` | *(none — default)* | Yes | Real infra, no business logic — `/healthz`/`/readyz`, CORS, structured logging |
| `frontend` | *(none — default)* | Yes | Real infra, minimal UI — one page, really calls the real API |
| `scheduler` | *(none — default)* | Yes | Stub — real DB+Redis connectivity, heartbeat log only |
| `worker-http` | *(none — default)* | Yes — **1 replica**, concurrency **2** (down from doc 13's original target of 2 replicas × 6) | Stub — real Celery worker on the real `http` queue, one no-op `ping` task |
| `mock-store` | *(none — default)* | Yes | Stub — placeholder page + health check |
| `worker-browser` | `browser` | No | Stub — still the same lightweight image as the other workers, **not** `Dockerfile.playwright` (doc 13's Phase-0 interim note still applies); one no-op `ping` task |
| `worker-notifications` | `workers` | No | Stub — one no-op `ping` task; Resend untouched, no API key needed |
| `worker-exports` | `workers` | No | Stub — one no-op `ping` task |
| `minio` | `storage` | No | Real, but nothing in Phase 0 requires it |
| `flower` | `monitoring` | No | Not part of Phase 0's acceptance criteria — deferred emphasis to Phase 8 (doc 15), same as before |
| `prometheus` | `monitoring` | No | ″ |
| `grafana` | `monitoring` | No | ″ |

Not in the compose file at all (unchanged): Caddy (production-only, doc 14), anything Stripe/billing.

**Two things this does *not* change:**
- `/readyz` was already designed to check only Postgres (via PgBouncer) and Redis (doc 11 §3) — MinIO was never in its dependency chain. Gating `minio` behind `storage` required no code change to satisfy "readiness checks must not require MinIO" — it was already true.
- **Production is unaffected.** These are Compose `profiles:` tags on the same service definitions doc 13 already specified — an untagged service always starts; a tagged one starts only when its profile is active. Production's `.env` sets `COMPOSE_PROFILES=browser,workers,storage,monitoring` (doc 14 §1), so a plain `docker compose up -d` on the VPS still brings up everything doc 00 §3 approved, at the same replica/concurrency numbers as before. The profile split is a local-dev convenience layer only.

---

## E. Local prerequisites

**All platforms**

- Git.
- Docker Compose **v2** (the `docker compose` subcommand, not the legacy standalone `docker-compose` binary) — everything runs containerized, so this is the one hard requirement.
- ~10 GB free disk space for images, volumes, and build cache (Phase 0's images are all lightweight — no Chromium yet, §H).
- *Optional, for local IDE convenience only — nothing requires this to run the stack:* Python 3.12 + [`uv`](https://docs.astral.sh/uv/) and Node.js 22 LTS + npm, if you want lint/type-check/editor autocomplete outside a container.

**Windows**

- Docker Desktop with the **WSL2 backend** enabled (Settings → General → "Use the WSL 2 based engine").
- Clone and work inside the WSL2 filesystem (e.g. `\\wsl$\Ubuntu\home\you\...`), **not** under `/mnt/c/...` — cross-filesystem bind mounts are noticeably slower and occasionally flaky with Docker Desktop on Windows.
- Run all commands in §F from a WSL2 shell (e.g. Ubuntu), not PowerShell/cmd.

**macOS**

- Docker Desktop (Apple Silicon or Intel build matching your Mac).
- Docker Desktop → Settings → Resources: allow at least 4 GB RAM (see §H) and the disk space above.

**Linux**

- Docker Engine + the `docker-compose-plugin` package (via your distro's package manager or `get.docker.com`) — gives you `docker compose` directly, no Docker Desktop needed.
- Add your user to the `docker` group (`sudo usermod -aG docker $USER`, then re-login) to run without `sudo`.

---

## F. Exact commands

```bash
# --- one-time setup ---
cp .env.example .env                     # fill in local placeholder values if you want non-defaults

# --- start: minimal default (recommended on this machine) ---
make up                                  # = docker compose up -d --build
docker compose up -d --build             # 10 services: postgres, pgbouncer, redis, mailhog, migrate,
                                          # api, frontend, scheduler, worker-http (x1), mock-store

# --- start: optional profiles — add only what you're actively working on ---
make up-browser                          # = docker compose --profile browser up -d --build
docker compose --profile browser up -d --build        # + worker-browser

make up-workers                          # = docker compose --profile workers up -d
docker compose --profile workers up -d                 # + worker-notifications, worker-exports

make up-storage                          # = docker compose --profile storage up -d
docker compose --profile storage up -d                  # + minio

make up-monitoring                       # = docker compose --profile monitoring up -d
docker compose --profile monitoring up -d                # + flower, prometheus, grafana

# combine profiles when you genuinely need more than one at once:
docker compose --profile browser --profile storage up -d --build

# --- verify healthy ---
make health                              # curls /healthz and /readyz, prints results
docker compose ps                        # default profile: 9 "running (healthy)" + migrate "exited (0)"

# --- test ---
make test                                # backend pytest + frontend vitest, both inside their containers
docker compose exec api pytest                       # backend only
docker compose exec api pytest -m integration          # just the /readyz integration test
docker compose exec frontend npm test                    # frontend only

# --- lint / type-check ---
make lint                                # ruff + eslint
make typecheck                           # mypy + tsc --noEmit

# --- manually trigger the default worker's stub task ---
docker compose exec worker-http celery -A app.workers.celery_app call app.workers.tasks_http.ping
docker compose logs worker-http --tail 20     # should show the task received and completed

# --- watch logs ---
docker compose logs -f api scheduler worker-http

# --- stop (keep data) ---
make stop                                # = docker compose stop
docker compose stop

# --- full reset (destroys volumes — fresh Postgres/Redis/MinIO) ---
make reset                               # = scripts/reset_dev_env.sh, asks for confirmation
docker compose down -v
```

`make up` never starts more than the 10-service minimal set — the `up-browser`/`up-workers`/`up-storage`/`up-monitoring` targets are strictly additive on top of whatever's already running, so you can layer on exactly the profile you're actively working with and leave the rest off.

---

## G. Phase 0 acceptance checklist

**Required — default (minimal) profile. Phase 1 doesn't start until every box here is checked.**

- [ ] `docker compose up -d --build` completes with no errors, from a clean clone with only `.env` filled in from `.env.example`.
- [ ] `docker compose ps` shows exactly these `running (healthy)`: `postgres`, `pgbouncer`, `redis`, `mailhog`, `api`, `frontend`, `scheduler`, `worker-http`, `mock-store`. `migrate` shows `exited (0)`. **Nothing else should be running** — confirms the default profile is actually minimal.
- [ ] `curl -f http://localhost:8000/healthz` → `200`.
- [ ] `curl -f http://localhost:8000/readyz` → `200` (Postgres through PgBouncer + Redis only — no MinIO dependency, §D).
- [ ] `http://localhost:3000` loads and shows the API status badge as reachable (frontend→API wiring, including CORS).
- [ ] `http://localhost:8025` (MailHog UI) reachable.
- [ ] `http://localhost:4000` (mock-store) returns the placeholder page; `/healthz` returns 200.
- [ ] `worker-http`'s stub task fires and completes when manually triggered (§F).
- [ ] `docker compose exec api pytest` — all pass, including the `/readyz` integration test.
- [ ] `docker compose exec frontend npm test` — all pass.
- [ ] `docker compose exec api ruff check .` / `mypy app` — clean.
- [ ] `docker compose exec frontend npx eslint .` / `npx tsc --noEmit` — clean.
- [ ] `docker compose down -v && docker compose up -d --build` — succeeds again from a fully clean state.
- [ ] CI (`.github/workflows/ci.yml`) is green on a pushed branch, including the smoke-test job (which exercises the default profile only — matching this machine's constraint, not the full stack).
- [ ] `git grep -i` for anything that looks like a real secret across tracked files returns nothing; `.env` confirmed **not** tracked by git.

**Optional — verify only if/when you activate that profile. Not required to close Phase 0.**

- [ ] `make up-browser` → `worker-browser` reaches `running`; its stub `ping` task fires and completes.
- [ ] `make up-workers` → `worker-notifications` and `worker-exports` both reach `running`; both stub `ping` tasks fire and complete.
- [ ] `make up-storage` → `minio` reaches `running (healthy)`; console reachable at `http://localhost:9001`.
- [ ] `make up-monitoring` → `flower` (`:5555`), `prometheus` (`:9090`), `grafana` (`:3001`) all reachable.

---

## H. Minimum local hardware requirements

**Default (minimal) profile — the one that matters for this machine:**

| Resource | Minimum | Comfortable |
|---|---|---|
| System RAM | 6 GB | 8 GB |
| Docker Engine/Desktop RAM allocation | 3 GB | 4 GB |
| Free disk | 8 GB | 15 GB |
| CPU | 2 cores | 4 cores (an i3's 2 cores/4 threads is workable, just slower on `--build`) |

Rough basis: Postgres ~250–300 MB, the Next.js **dev** server ~300–400 MB (heaviest single piece in the default set — dev mode with hot-reload is heavier than a production build), four lightweight Python processes (`api`/`scheduler`/`worker-http`/`mock-store`) at ~80–150 MB each, Redis/PgBouncer/MailHog under 100 MB combined. Total actual usage **≈1–1.2 GB** — meaningfully lighter than before, since MinIO, the two extra workers, and the whole monitoring trio no longer run by default. The gap to the "3 GB Docker allocation" figure is headroom for `--build` layers and running tests alongside the stack, not the services themselves.

**Incremental cost if/when you turn on an optional profile:**

| Profile | Adds | Rough extra RAM |
|---|---|---|
| `storage` | minio | ~150 MB |
| `workers` | worker-notifications + worker-exports | ~150–200 MB combined |
| `monitoring` | flower + prometheus + grafana | ~300–400 MB combined |
| `browser` | worker-browser (still no Chromium in Phase 0, §D) | ~80–150 MB *for now* — see below |

**What changes once Playwright lands (a later phase, not Phase 0):** once `worker-browser` switches to `Dockerfile.playwright` and starts actually rendering pages, the `browser` profile alone gets meaningfully heavier — a single headless Chromium context commonly runs 300–500 MB+ RAM while active, so budget roughly **+1–1.5 GB** on top of the default footprint whenever `browser` is active from that point on. On an 8 GB machine, running the default profile plus `browser` at the same time will likely get tight — treat that as a cue to test browser-rendering work in short sessions (`make up-browser`, test, `docker compose --profile browser stop`) rather than leaving it up alongside everything else all day. Nothing to act on now; just the honest expectation for later.

---

## What happens after you approve this

I'll generate the 40 files above, in this order: root config (`.env.example`, `.gitignore`) → `backend/` → `tools/mock-store/` → `frontend/` → `docker-compose.yml` → `Makefile`/`scripts/` → `.github/workflows/ci.yml` → `README.md` — then walk through §F myself, confirm every box in §G, and report back with the actual results (not just "should work"), before calling Phase 0 done.
