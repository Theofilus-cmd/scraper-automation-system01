# 13 — Docker Compose Dev Setup

This is the target Compose spec — it gets materialized as an actual runnable `docker-compose.yml` in Phase 0 of the roadmap (doc 15), once there's a minimal `backend/Dockerfile` and `frontend/Dockerfile` for it to reference. Publishing it here first means Phase 0 is "wire up what's already agreed," not a fresh design decision.

## 1. Services

**Local-dev resource profile (doc 16, added 2026-08-24):** `minio`, `worker-browser`, `worker-notifications`, `worker-exports`, `flower`, `prometheus`, and `grafana` are tagged with Compose `profiles:` (`storage`, `browser`, `workers`, `workers`, `monitoring` ×3, respectively) so a plain `docker compose up` starts only the lightweight default set — everything else is opt-in via `--profile <name>`. This is a local-development convenience only; production activates every profile by default (doc 14 §1). Full rationale and the default/optional split: doc 16 §D.

```yaml
# docker-compose.yml (target — materialized in Phase 0)
version: "3.9"

x-backend-build: &backend-build
  context: ./backend
  dockerfile: Dockerfile

services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-scraper}
      POSTGRES_USER: ${POSTGRES_USER:-scraper}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set in .env}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-scraper}"]
      interval: 10s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits: {cpus: "1.0", memory: 1g}
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}

  pgbouncer:
    image: edoburu/pgbouncer:latest
    restart: unless-stopped
    environment:
      DATABASE_URL: postgres://${POSTGRES_USER:-scraper}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-scraper}
      POOL_MODE: transaction
      MAX_CLIENT_CONN: 200
      DEFAULT_POOL_SIZE: 20
    depends_on:
      postgres: {condition: service_healthy}
    ports: ["6432:6432"]

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --appendonly yes --appendfsync everysec
    volumes: ["redis_data:/data"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits: {cpus: "0.5", memory: 512m}

  minio:
    profiles: ["storage"]             # opt-in — doc 16 §D; nothing in Phase 0 requires it (readyz never checks it)
    image: minio/minio:latest
    restart: unless-stopped
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER:-minioadmin}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:?set in .env}
    volumes: ["minio_data:/data"]
    ports: ["9000:9000", "9001:9001"]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 10s
      timeout: 5s
      retries: 5

  mailhog:                          # local email capture — no real email sent in dev
    image: mailhog/mailhog:latest
    restart: unless-stopped
    ports: ["8025:8025"]

  migrate:                          # one-shot: runs, exits, gates every dependent service
    build: *backend-build
    command: alembic upgrade head
    env_file: .env
    depends_on:
      postgres: {condition: service_healthy}
    restart: "no"

  api:
    build: *backend-build
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    env_file: .env
    depends_on:
      migrate: {condition: service_completed_successfully}
      pgbouncer: {condition: service_started}
      redis: {condition: service_healthy}
    ports: ["8000:8000"]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/healthz"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "1.0", memory: 512m}
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}

  scheduler:                        # doc 04 §4.1 — DB-polling schedule claimer, not Celery Beat
    build: *backend-build
    command: python -m app.scheduler.main
    env_file: .env
    depends_on:
      migrate: {condition: service_completed_successfully}
      redis: {condition: service_healthy}
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "0.5", memory: 256m}

  worker-http:
    build: *backend-build
    command: celery -A app.workers.celery_app worker -Q http -c ${HTTP_WORKER_CONCURRENCY:-2} --hostname=http@%h
    env_file: .env
    depends_on:
      migrate: {condition: service_completed_successfully}
      redis: {condition: service_healthy}
    restart: unless-stopped
    deploy:
      replicas: 1                    # Phase 0 local-dev default (doc 16 §D) — production restores the
                                      # doc 00 §3 target of 2 replicas via docker-compose.prod.yml (doc 14 §1)
      resources:
        limits: {cpus: "1.0", memory: 512m}

  worker-browser:
    profiles: ["browser"]                 # opt-in — doc 16 §D; Phase 0 still builds from backend/Dockerfile, not this one (see the interim note below)
    build:
      context: ./backend
      dockerfile: Dockerfile.playwright   # separate image: Chromium + deps, kept out of the lean HTTP-worker image
    command: celery -A app.workers.celery_app worker -Q browser -c ${BROWSER_WORKER_CONCURRENCY:-2} --hostname=browser@%h
    env_file: .env
    depends_on:
      migrate: {condition: service_completed_successfully}
      redis: {condition: service_healthy}
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "1.5", memory: 1.5g}   # heaviest single service — Chromium contexts

  worker-notifications:               # doc 00 §3 "email/alert worker" — its own queue so alert delivery
    profiles: ["workers"]             # opt-in — doc 16 §D
    build: *backend-build             # never queues behind scraping load, and vice versa
    command: celery -A app.workers.celery_app worker -Q notifications -c 2 --hostname=notify@%h
    env_file: .env
    depends_on:
      migrate: {condition: service_completed_successfully}
      redis: {condition: service_healthy}
    restart: unless-stopped

  worker-exports:
    profiles: ["workers"]             # opt-in — doc 16 §D
    build: *backend-build
    command: celery -A app.workers.celery_app worker -Q exports -c 2 --hostname=export@%h
    env_file: .env
    depends_on:
      migrate: {condition: service_completed_successfully}
      redis: {condition: service_healthy}
    restart: unless-stopped

  frontend:
    build: {context: ./frontend}
    command: npm run dev
    environment:
      NEXT_PUBLIC_API_URL: http://localhost:8000/api/v1
    ports: ["3000:3000"]
    depends_on: [api]

  mock-store:                         # doc 12 §7 — local fixture site for adapter dev + E2E, no real internet calls
    build: {context: ./tools/mock-store}
    ports: ["4000:4000"]

  # --- optional, behind `--profile monitoring` (doc 00 §4 item, recommended but not required to run the stack) ---
  flower:
    build: *backend-build
    command: celery -A app.workers.celery_app flower --port=5555
    env_file: .env
    depends_on: [redis]
    ports: ["5555:5555"]
    profiles: ["monitoring"]

  prometheus:
    image: prom/prometheus:latest
    volumes: ["./infra/prometheus.yml:/etc/prometheus/prometheus.yml:ro"]
    ports: ["9090:9090"]
    profiles: ["monitoring"]

  grafana:
    image: grafana/grafana:latest
    ports: ["3001:3000"]
    profiles: ["monitoring"]

volumes:
  postgres_data:
  redis_data:
  minio_data:
```

**Phase 0 interim note (doc 16):** to avoid installing Playwright/Chromium before anything actually uses it, Phase 0's `worker-browser` builds from the same lightweight `backend/Dockerfile` as the other workers instead of `Dockerfile.playwright` shown above, and runs a no-op stub task. `Dockerfile.playwright` and real browser rendering land together in whichever later phase first needs JS-rendered pages — at that point `worker-browser`'s `build.dockerfile` line changes to match this doc, and nothing else about its wiring (queue name, resource limits, dependency graph) needs to change.

## 2. `.env.example`

```dotenv
# --- Postgres ---
POSTGRES_DB=scraper
POSTGRES_USER=scraper
POSTGRES_PASSWORD=change-me-locally

# --- MinIO (S3-compatible) ---
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=change-me-locally
S3_ENDPOINT_URL=http://minio:9000
S3_BUCKET=scraper-artifacts

# --- App ---
DATABASE_URL=postgresql+asyncpg://scraper:change-me-locally@pgbouncer:6432/scraper
REDIS_URL=redis://redis:6379/0
JWT_SIGNING_KEY=dev-only-change-in-prod
STRIPE_API_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
SMTP_HOST=mailhog
SMTP_PORT=1025

# --- Worker tuning (doc 00 §3 / doc 04 §3 — all configurable, none hardcoded) ---
HTTP_WORKER_CONCURRENCY=6
BROWSER_WORKER_CONCURRENCY=2
DOMAIN_MAX_CONCURRENCY=3
DOMAIN_RATE_PER_SECOND=0.5
WORKSPACE_FAIRNESS_MAX_INFLIGHT=20
TASK_MAX_ATTEMPTS=3
```

`.env.example` is committed; `.env` is gitignored, and `POSTGRES_PASSWORD`/`MINIO_ROOT_PASSWORD`/`JWT_SIGNING_KEY`/`STRIPE_API_KEY` have no default (the `:?set in .env` syntax fails the container start rather than silently booting with a blank secret) — never hardcode secrets, per the constraint, applies to "no insecure default" too, not just "not committed."

## 3. Day-to-day dev workflow

```bash
cp .env.example .env                       # first time only, then fill in local values
docker compose up -d --build
docker compose exec api alembic upgrade head   # also runs automatically via the `migrate` service on `up`
docker compose exec api python scripts/seed_demo.py   # doc 12 §6 — puts a working demo project in the UI immediately
docker compose --profile monitoring up -d       # optional: Flower/Prometheus/Grafana
docker compose exec api pytest                  # backend tests
docker compose exec frontend npm test             # frontend tests
docker compose logs -f worker-http worker-browser  # tail scraping activity
docker compose down -v                              # full teardown including volumes, for a clean slate
```

`mailhog` (http://localhost:8025) is where every alert/verification email lands in dev — nothing is ever sent to a real address locally. `minio` console (http://localhost:9001) shows exports and any failure-sample artifacts. `flower` (http://localhost:5555, monitoring profile) shows live queue/task state, which is the fastest way to see the scheduler → coordinator → worker flow (doc 04 §5) actually happening during development.
