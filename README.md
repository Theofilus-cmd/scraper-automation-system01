# Scraper Automation System

SaaS-style price/stock monitoring platform. Users register, add a public
product URL (mock store or a Shopify storefront), and the system scrapes it
on demand or on a schedule, stores the current observation plus an
append-only price/stock history, detects changes, records alerts, and sends
alert emails. A multi-page Next.js UI shows sources, runs, price charts,
alerts and dead-lettered tasks.

Everything runs under Docker Compose. See `docs/design/` for the full
architecture and `PROJECT_CONTEXT.md` for the current implementation state.

## Architecture

| Service         | Tech                        | Role                                                         |
| --------------- | --------------------------- | ------------------------------------------------------------ |
| `frontend`      | Next.js (port 3000)         | Login/register, sources, runs, charts, alerts, dead-letter   |
| `api`           | FastAPI (port 8000)         | Auth, sources, runs, products, alerts, tasks, business data  |
| `scheduler`     | Python service              | Heartbeat, claims due schedules, reconciliation, retention   |
| `worker-http`   | Celery (queue `http`)       | Fetch + parse + normalize + persist, alert detection + email |
| `postgres`      | PostgreSQL 16 (+ PgBouncer :6432) | Application data                                       |
| `redis`         | Redis 7                     | Celery broker, execution lease, per-domain politeness, auth rate limits |
| `mailhog`       | MailHog (UI :8025)          | Dev SMTP sink for alert emails                               |
| `mock-store`    | FastAPI (port 4000)         | Deterministic local scrape target                            |
| `migrate`       | Alembic (one-shot)          | Runs `alembic upgrade head` (migrations 0001-0007)           |

Scrape flow: `POST /sources/{id}/runs` (or a due schedule) creates a run and
a durable task -> Celery `http` queue -> worker takes a Redis execution lease,
applies domain politeness (semaphore, rate limit, cooldown), fetches through
the SSRF-hardened fetcher, runs the adapter (`mock_store` or `shopify`),
normalizes and validates, then in ONE transaction upserts the product,
updates `current_observations`, appends `observation_history` when values
changed, and writes `alerts` rows (unique `dedupe_key`). Alert emails are
then sent via SMTP (claim-then-send). Transient failures retry with jittered
backoff (honouring `Retry-After`); persistent failures end dead-lettered.

### Adapters

- `mock_store` - schema.org JSON-LD pages from the local mock store.
- `shopify` - URL must be a Shopify-shaped path: `/products.json`,
  `/collections/<c>`, or `/products/<handle>`. Reads the public JSON
  endpoints, falling back to JSON-LD. `products.json` has no currency, so
  the adapter reads it from the shop's `/meta.json` and falls back to `USD`.
  **A source tracks one product**: for a multi-product URL such as
  `/products.json?limit=20`, only the first product is persisted.
  Use `/products/<handle>` to track a specific product.

## First-time setup (bootstrap)

This repo intentionally does **not** commit `backend/uv.lock`,
`tools/mock-store/uv.lock`, or `frontend/package-lock.json` until you
generate them yourself, once. Every Dockerfile installs dependencies with
a frozen/reproducible command (`uv sync --frozen`, `npm ci`) so that once
a lockfile exists, every future build resolves the exact same versions —
but that also means those builds fail on purpose if the lockfile is
missing. `make up` checks for all three first and tells you to run
`make bootstrap` rather than failing with a confusing Docker error.

Pick **Option A** unless you have a specific reason to install Python/uv/
Node/npm on this laptop directly — it needs nothing but Docker.

### Option A — Docker-first (recommended)

Requires only **Docker Desktop + WSL2 + Git** on the host — no Python,
uv, Node.js, or npm installed locally.

```bash
git clone <this repo> && cd scraper-automation-system
make bootstrap
```

`make bootstrap` does three things, each in a short-lived container that
is removed immediately after (nothing is left running):

1. Copies `.env.example` to `.env` if `.env` doesn't already exist.
2. Runs `uv lock` inside `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`
   (matches `backend/Dockerfile`'s own Python version), once for
   `backend/` and once for `tools/mock-store/`. Each container has that
   directory bind-mounted in, so the `uv.lock` it resolves is written
   straight to your working copy — nothing is generated inside the
   container and then lost when it exits.
3. Runs `npm install --package-lock-only` inside `node:22-alpine`
   (matches `frontend/Dockerfile`'s base image) with `frontend/`
   bind-mounted the same way, writing `frontend/package-lock.json`
   directly to your working copy. `--package-lock-only` resolves and
   writes the lockfile without also installing `node_modules` on the
   host, so nothing is left behind to clean up.

Review what got generated, then commit it — this is a one-time step,
not something that runs on every build:

```bash
git add backend/uv.lock tools/mock-store/uv.lock frontend/package-lock.json
git commit -m "Add generated lockfiles"
```

From this point on, `make up` (and CI) reuse the committed lockfiles via
`uv sync --frozen` / `npm ci` — reproducible, and no network dependency
resolution happens at build time anymore, only package downloads for the
exact versions already pinned in the lockfiles.

### Option B — Host tools

Install Python 3.12, uv, Node.js 22, and npm directly in your WSL2
distro, then generate the lockfiles the normal way.

```bash
# uv (also manages Python for you -- no system Python/apt package needed)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc                    # or open a new shell, so `uv` is on PATH
uv python install 3.12

# Node.js 22 + npm, via nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.6/install.sh | bash
source ~/.bashrc                    # or open a new shell, so `nvm` is on PATH
nvm install 22
nvm use 22
node --version                      # confirm v22.x
npm --version
```

Then generate each lockfile from the host, in each project directory:

```bash
cd backend && uv lock && cd ..
cd tools/mock-store && uv lock && cd ../..
cd frontend && npm install && cd ..     # npm install (not npm ci) creates
                                          # package-lock.json if it's missing
```

All three generated files must be committed, same as Option A:

```bash
git add backend/uv.lock tools/mock-store/uv.lock frontend/package-lock.json
git commit -m "Add generated lockfiles"
```

`npm install` also populates `frontend/node_modules` on the host —
harmless (git-ignored, and the Docker build installs its own copy inside
the image regardless), delete it if you'd rather not have it sitting
there: `rm -rf frontend/node_modules`.

## Quickstart

Run after the one-time bootstrap above.

```bash
make up          # docker compose up -d --build (default profile)
make health      # curls /healthz and /readyz
```

Default services: postgres, pgbouncer, redis, mailhog, migrate (one-shot),
api, scheduler, worker-http, mock-store, frontend.

- Frontend: http://localhost:3000
- API: http://localhost:8000 (`/healthz`, `/readyz`, interactive docs at `/docs`)
- MailHog UI: http://localhost:8025
- Mock store: http://localhost:4000

### Try it end to end

```bash
API=http://localhost:8000/api/v1
TOKEN=$(curl -s -X POST $API/auth/register -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"Passw0rd!long123","display_name":"Me"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"]["access_token"])')

# Public Shopify storefront (needs outbound internet)
SID=$(curl -s -X POST $API/sources -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.allbirds.com/products.json?limit=20"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -s -X POST $API/sources/$SID/runs -H "authorization: Bearer $TOKEN"   # 202
sleep 10
curl -s $API/products -H "authorization: Bearer $TOKEN"
```

Then open http://localhost:3000 and log in with the same account. Use a URL
such as `http://mock-store:4000/products/widget-in-stock` for the offline mock store.

### SSRF allowlist and public stores

`SSRF_ALLOWED_HOSTS` (default `mock-store`) only **exempts** the listed hosts
from the private/loopback-IP block; public hosts are never blocked by it.
Set it empty (`SSRF_ALLOWED_HOSTS=`) outside development.

### Alerts

When a scrape finds a different price or stock status than the stored
observation, an `alerts` row (`price_changed` / `stock_status_changed`) is
written and an email is sent to the workspace owner via SMTP (MailHog in dev:
`SMTP_HOST=mailhog`, `SMTP_PORT=1025`, toggle with `ALERTS_EMAIL_ENABLED`).
Read them at `GET /api/v1/alerts` or in the MailHog UI.

## API (all under `/api/v1`, Bearer JWT unless noted)

| Method & path | Purpose |
| --- | --- |
| `POST /auth/register`, `POST /auth/login` (no auth, rate limited), `GET /auth/me` | Accounts |
| `POST /sources` | Create/get a source (201 new / 200 existing); 422 `UNSUPPORTED_ADAPTER` if no adapter matches |
| `GET /sources`, `GET/PATCH/DELETE /sources/{id}`, `POST /sources/{id}/unarchive` | List, read, pause/activate (`status`), archive |
| `PUT/DELETE /sources/{id}/schedule` | Set (`interval_minutes`, `is_active`) / remove schedule |
| `POST /sources/{id}/runs` | Manual run (202; optional `Idempotency-Key` header) |
| `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/tasks` | Runs and their tasks |
| `GET /tasks` | Tasks (incl. failed / dead-lettered, with `error_reason`) |
| `GET /products`, `GET /products/{id}`, `GET /products/{id}/history` | Products, current observation, price/stock history |
| `GET /alerts` | Detected changes (`kind`, email status) |
| `POST /scrapes`, `GET /scrapes/{task_id}` | Legacy single-URL scrape aliases |
| `POST/GET /business-data/sources`, `GET /business-data/sources/{id}`, `POST .../ingest`, `GET .../runs`, `GET .../records`, `GET .../records/{rid}/history` | Business-data hub |

List endpoints use cursor pagination (`cursor`, `limit`) and return
`{"data": [...], "pagination": {"next_cursor", "has_more"}}`.

## Frontend pages

`/` (login/register, summary tiles for alerts and dead-lettered tasks,
sources list, add source), `/sources/[id]` (schedule, manual run, runs,
recent alerts, price/stock history charts), `/runs/[id]` (run + task
detail), `/alerts` (filter by kind), `/dead-letter`.

## Database migrations

Alembic, `backend/app/db/migrations/versions/`, applied automatically by the
`migrate` service (or `make migrate`):

| Rev  | Content |
| ---- | ------- |
| 0001 | Enable extensions |
| 0002 | Core scraping tables (sources, products, current_observations) |
| 0003 | Source lifecycle, schedules, runs/tasks, `observation_history` (immutable) |
| 0004 | Users, workspaces, workspace members, source tenancy |
| 0005 | Business-data hub tables |
| 0006 | Allow `shopify` in `sources.adapter_type` |
| 0007 | `alerts` (unique `dedupe_key`, email delivery state) |

## Optional profiles

Four additional service groups are opt-in via Docker Compose profiles, so
they only start (and only consume RAM/CPU) when explicitly requested:

| Profile      | Services                                | Enable with        |
| ------------ | ---------------------------------------- | ------------------- |
| `browser`    | worker-browser                           | `make up-browser`    |
| `workers`    | worker-notifications, worker-exports     | `make up-workers`    |
| `storage`    | minio                                    | `make up-storage`    |
| `monitoring` | flower, prometheus, grafana              | `make up-monitoring` |

Combine profiles by chaining `--profile` flags:

```bash
docker compose --profile browser --profile workers up -d --build
```

None are needed for the core flow. `worker-browser`, `worker-notifications`
and `worker-exports` are still stubs (ping tasks only); alert emails are
sent by `worker-http`. `/readyz` checks only Postgres and Redis.

## Prerequisites

### Windows (WSL2)

1. Install WSL2: run `wsl --install` in an elevated PowerShell, then reboot.
2. Install Docker Desktop and enable **Use the WSL 2 based engine**, plus
   WSL integration for your distro (Settings -> Resources -> WSL
   Integration).
3. Open your WSL2 distro's terminal (not PowerShell) for every command in
   this README -- `git`, `make`, and `docker` should all run from inside
   WSL2.
4. Clone the repo inside the WSL2 filesystem (e.g.
   `~/scraper-automation-system`), not under `/mnt/c/...`, for acceptable
   filesystem performance.
5. Verify with `docker compose version` and `make --version`.

### macOS

1. Install Docker Desktop for Mac.
2. Install `make` (comes with the Xcode Command Line Tools:
   `xcode-select --install`).
3. Clone the repo and run the commands below from Terminal.

### Linux

1. Install Docker Engine plus the Docker Compose plugin.
2. Install `make` via your package manager (e.g. `apt install make`).
3. Ensure your user is in the `docker` group, or prefix commands with `sudo`.

## Commands

| Command              | What it does                                        |
| --------------------- | ---------------------------------------------------- |
| `make bootstrap`      | One-time: generate lockfiles via Docker (Option A above) |
| `make up`             | Start the default (minimal) profile                  |
| `make up-browser`     | Also start worker-browser                             |
| `make up-workers`     | Also start worker-notifications, worker-exports       |
| `make up-storage`     | Also start MinIO                                      |
| `make up-monitoring`  | Also start Flower, Prometheus, Grafana                |
| `make health`         | Curl `/healthz` and `/readyz`                         |
| `make logs`           | Tail logs for all running services                    |
| `make test`           | Run backend + frontend test suites                    |
| `make test-backend`   | Run backend tests only (`pytest`)                     |
| `make test-frontend`  | Run frontend tests only (`vitest`)                    |
| `make lint`           | Run `ruff` and `eslint`                               |
| `make typecheck`      | Run `mypy` and `tsc --noEmit`                         |
| `make migrate`        | Run Alembic migrations manually                       |
| `make stop`           | `docker compose down` (keeps volumes)                 |
| `make reset`          | Confirm, then `docker compose down -v` (wipes volumes) |

## Hardware

The default (minimal) profile is sized for constrained hardware:

- System RAM: 6 GB minimum, 8 GB comfortable
- Docker Desktop / Engine memory allocation: 3 GB minimum, 4 GB comfortable
- Steady-state usage: roughly 1-1.5 GB across the default services

Each optional profile adds incremental cost on top of that.

## Project structure

```
backend/      FastAPI API, scheduler, Celery workers, Alembic migrations
frontend/     Next.js app
tools/        Local-only auxiliary services (mock-store)
scripts/      Dev-environment helper scripts
docs/design/  Architecture and planning documents
```

## Tests

```bash
make test-backend    # docker compose exec api pytest  (unit + real Postgres/Redis integration)
make test-frontend   # vitest
make lint && make typecheck
```

Rebuild images (`docker compose up -d --build api worker-http scheduler`)
first so the containers contain your latest code and tests.

## Contributing

Larger changes should be reflected in `docs/design/` and `PROJECT_CONTEXT.md`.
