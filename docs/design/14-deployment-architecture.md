# 14 — Deployment Architecture

Confirmed target (doc 00 §3): single VPS + Docker Compose for production, explicitly **not** Kubernetes for v1, with every item on your required list below addressed and a documented path to grow past one VPS when it's actually needed.

## 1. Topology

One VPS running the full stack from doc 13 §1, via a layered `docker-compose.prod.yml` on top of the base file (same services, production-appropriate overrides — not a parallel config to keep in sync by hand). Doc 13's services are split across a minimal default and four opt-in Compose profiles for local-dev convenience on constrained hardware (doc 16 §D) — production's `.env` sets `COMPOSE_PROFILES=browser,workers,storage,monitoring`, so a plain `docker compose up -d` here still brings up the full doc 00 §3-approved topology, unchanged:

```
Internet
   │  :443 / :80
   ▼
┌─────────────┐
│    Caddy      │  reverse proxy + automatic HTTPS (Let's Encrypt)
└──────┬──────┘
       │
   ┌───┴────────────────────┐
   ▼                        ▼
frontend (Next.js, built)   api (FastAPI/Uvicorn)
                                  │
                    ┌─────────────┼──────────────┐
                    ▼             ▼              ▼
              postgres+pgbouncer  redis      minio *or* external S3-compatible bucket
                    ▲
        scheduler, worker-http(×2), worker-browser, worker-notifications, worker-exports
```

Starting VPS sizing recommendation: **4 vCPU / 8GB RAM** (e.g. a mid-tier Hetzner/DigitalOcean/Linode instance) — comfortably covers the worker pool sizing in doc 00 §3/doc 13 §1 (2 HTTP workers × concurrency 6, 1 browser worker × concurrency 2, plus API/Postgres/Redis) with headroom. Vertical scaling (bigger VPS) is the first, cheapest lever before any architectural change — see §7.

## 2. Reverse proxy & HTTPS

Caddy over Nginx+Certbot for v1: automatic certificate provisioning/renewal with a few lines of config, one less moving part to operate on a single box.

```
# Caddyfile
app.yourdomain.com {
    reverse_proxy frontend:3000
}

api.yourdomain.com {
    reverse_proxy api:8000
}
```

Swapping to Nginx later (e.g. if you need request-level features Caddy doesn't cover) doesn't touch anything behind it — both just proxy to the same container names on the internal Compose network.

## 3. What every service gets, by default (not opt-in)

| Requirement | Implementation |
|---|---|
| Health checks | Every long-running service has a Compose `healthcheck` (doc 13 §1); Caddy only routes to `api`/`frontend` once healthy |
| Structured logs | JSON to stdout everywhere (doc 11 §1) |
| Resource limits | `deploy.resources.limits` per service (doc 13 §1) — sized so one runaway service (most likely `worker-browser`) can't starve Postgres/Redis of RAM |
| Restart policies | `restart: unless-stopped` on every long-running service; `migrate` is `restart: "no"` (one-shot, must not loop-crash the deploy) |
| Docker log rotation | `json-file` driver, `max-size: 10m`, `max-file: 3` per service — bounds disk usage from logs without needing a log shipper for v1 |
| Environment-based config | Every service reads from `.env` (doc 13 §2); no config baked into images |
| Secure secrets handling | `.env` is `chmod 600`, owned by the deploy user, never committed (`.gitignore`'d), never baked into an image layer; required secrets have no default (`:?` syntax fails-closed, doc 13 §2); rotation = update `.env`, `docker compose up -d` recreates only the services whose config changed |
| DB migrations | `migrate` service runs `alembic upgrade head` and must exit 0 before `api`/workers start (`condition: service_completed_successfully`, doc 13 §1) — a bad migration blocks the deploy instead of the app starting against a half-migrated schema |

## 4. Secrets handling detail

For a single-VPS deployment, a root-only-readable `.env` file is the pragmatic choice over standing up a secrets manager (Vault, etc.) — that's real operational overhead this scale doesn't justify yet. What's non-negotiable regardless of scale:

- No secret ever appears in a Dockerfile, image layer, git history, or log line.
- `JWT_SIGNING_KEY`, `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, DB/MinIO passwords are generated fresh for production (never the same values as `.env.example` or local dev).
- If/when this moves to a managed platform or Kubernetes (§7), `.env` maps directly onto that platform's native secrets mechanism (env vars either way) — no redesign needed, just a different place to store the same values.

## 5. Backups

- **Automated:** a small `backup` container (or host cron calling `docker compose exec postgres pg_dump`) runs nightly, `pg_dump --format=custom` piped to the object-storage bucket (a separate prefix from user exports/artifacts). Retention: daily dumps kept 14 days, one weekly dump kept 8 weeks, both pruned automatically by the same job.
- Stored on the **external S3-compatible bucket**, not on the VPS's own disk — a backup that lives on the same disk it's backing up doesn't survive the failure modes that actually matter (disk failure, accidental `docker volume rm`).
- **Restore runbook** (tested, not just written — run it against staging periodically so it's not a surprise the first time it's needed for real):
  1. Provision/identify the target Postgres instance (fresh VPS or the same one after a disk failure).
  2. `docker compose up -d postgres redis minio` only — leave `api`/workers/`frontend` down.
  3. Download the desired dump from object storage.
  4. `pg_restore --clean --if-exists -d <connection-string> <dump-file>`.
  5. `docker compose run --rm migrate` — brings schema to current `head` in case the restored dump predates a later migration.
  6. Spot-check: query a known workspace/record, confirm row counts are sane.
  7. `docker compose up -d` (bring everything else back).
  8. Post-restore: check `usage_counters` for the affected period against Stripe's own records (doc 09 §3) before trusting billing numbers from a restored window.

## 6. Uptime & disk monitoring

Two independent, deliberately simple layers for v1 (the fuller Prometheus/Grafana stack from doc 13 §1 is available behind `--profile monitoring` whenever you want deeper metrics, but isn't required to know the basics):

- **Uptime:** an external checker (any hosted uptime-monitor service, free tier is sufficient at this scale) polling `https://api.yourdomain.com/healthz` and `https://app.yourdomain.com` from outside the VPS — this is the only way to know the box itself is reachable, since anything running on the VPS can't tell you the VPS is down.
- **Disk:** a host cron script (`df`-based, no extra service needed) alerting by email if any volume crosses 80%/90% thresholds — the most common single cause of a quietly-failing VPS (Postgres/Redis refusing writes once the disk fills) and the cheapest thing to guard against.

## 7. Environments & the scaling path

- **Local dev:** doc 13, full stack on a laptop.
- **Staging:** ✅ decided (doc 00 §4) — **no separate staging VPS yet.** Local Docker Compose (doc 13) is the dev *and* test environment for now: the same stack, migration, seed, and test commands, exercised locally before anything reaches production. When a real staging VPS is warranted, adding one is mechanical:
  1. Provision a second, smaller VPS.
  2. Copy `docker-compose.yml` + `docker-compose.prod.yml` unchanged.
  3. Create a separate `.env` (own domain, own Stripe *test-mode* keys, own DB/Redis/object-storage credentials — never share secrets between staging and prod).
  4. Point a `staging.yourdomain.com` DNS record at it; Caddy provisions its own certificate automatically (§2).
  5. Deploy from a `staging` branch/tag; promote to production by redeploying the same tested image tag against the prod `.env`.

  Reasonable triggers to revisit this: the team grows past ~1–2 people (so "break it locally" stops being low-cost), or a production incident occurs that a pre-prod smoke test would have caught.
- **Production:** as above.
- **Deploy procedure:** build/tag images by git SHA (`registry/api:abc1234`), `docker compose pull && docker compose up -d` on the target host. Keep the last several tags available in the registry.
- **Rollback procedure:** redeploy the previous known-good tag the same way. The one genuinely risky part is the database: **always take a fresh backup immediately before running a migration in production**, and prefer forward-fixing a bad migration over `alembic downgrade` — only run a downgrade for a migration explicitly written to be safely reversible. A code rollback against a schema that's moved forward is usually safer than a schema rollback.
- **Migration path once one VPS isn't enough** (in the order you'd actually reach for them — cheapest/least-disruptive first):
  1. **Vertically scale** the VPS (more CPU/RAM) — zero architecture change, the first lever for a while given this system's modest baseline footprint.
  2. **Split workers onto a second VPS** (scheduler + all workers on VPS #2, API + Postgres + Redis stay on VPS #1) — already possible with zero code change, since workers only need network access to Postgres/Redis/object storage, not to the API (doc 04 §2 module boundary is what makes this free).
  3. **Move Postgres to a managed database service** (removes backup/patching/failover ops burden, unlocks read replicas if read load ever justifies it).
  4. **Move Redis to a managed service** similarly, once broker traffic alone justifies isolating it (doc 04 §4.6).
  5. **Object storage:** if still self-hosted MinIO at this point, move to a real external S3-compatible provider (should already be true from launch per §1's recommendation).
  6. **Only then** consider a managed container platform or Kubernetes — worthwhile once you're operating enough VPSes by hand that orchestration itself becomes the bottleneck, not before. Nothing above requires it as a prerequisite.

Every stage above is possible without a rewrite specifically because of decisions made in doc 04 (stateless workers, no filesystem coupling, queue/DB as the only shared state) — the "keep the migration path open" requirement was a design input from day one, not something bolted on here.
