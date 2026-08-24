# Scraper Automation System

Phase 0 walking skeleton: a runnable, health-checked Docker Compose stack
with no scraping logic yet. It exists to prove the wiring -- database,
cache, queue, API, frontend, migrations, and worker processes -- all boot
and talk to each other correctly before any product feature is built on
top of it.

See `docs/design/` for the full architecture, and
`docs/design/16-phase-0-walking-skeleton-plan.md` specifically for the
plan this skeleton implements.

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

## Quickstart (lightweight default)

The default profile starts only the 10 services needed to pass the Phase
0 acceptance checklist, chosen to run comfortably on a low-RAM laptop.
Run this after the one-time bootstrap above.

```bash
make up
make health
```

`make up` runs `docker compose up -d --build` (after confirming all three
lockfiles exist). `make health` curls both `/healthz` and `/readyz` on
the API.

Once healthy:

- API: http://localhost:8000/healthz and http://localhost:8000/readyz
- Frontend: http://localhost:3000
- MailHog UI: http://localhost:8025
- Mock store: http://localhost:4000

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

None of these are required for Phase 0 sign-off. `/readyz` never checks
MinIO, and the browser/notifications/exports workers are not part of the
default acceptance criteria (see doc 16 for the full reasoning).

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
- Observed steady-state usage: roughly 1-1.2 GB across all 10 default services

Each optional profile adds incremental cost on top of that -- see doc 16
§H for the full breakdown. A future phase adding real Playwright/Chromium
browser automation will raise the `browser` profile's RAM needs
substantially; the current `worker-browser` stub uses the same
lightweight image as the other workers.

## Project structure

```
backend/      FastAPI API, scheduler, Celery workers, Alembic migrations
frontend/     Next.js app
tools/        Local-only auxiliary services (mock-store)
scripts/      Dev-environment helper scripts
docs/design/  Architecture and planning documents
```

## Contributing

This is Phase 0 of a phased build. Do not add Phase 1+ functionality
(real scraping, auth, billing, etc.) without an approved design update in
`docs/design/`.
