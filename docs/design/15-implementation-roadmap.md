# 15 — Phased Implementation Roadmap

Per your implementation rules: nothing below gets built until you approve the design docs (doc 00 §4 open items especially). Each phase is a vertical slice that compiles, runs, and is independently demoable — never "half an architecture layer." Every phase's exact file paths and diffs get proposed at the start of that phase, not now; what follows is the scope and sequencing.

| Phase | Goal | Builds on |
|---|---|---|
| **0. Walking skeleton** | `docker compose up` brings up every service from doc 13 (empty/stub app code), all health checks green, CI running lint/type/secret-scan on an empty-ish repo, Alembic wired with a first no-op migration. Nothing functional yet — this phase exists so every later phase starts from "it runs," not "it compiles for the first time." | doc 13, doc 12 §5 |
| **1. Free scraper (Mode 1)** | The smallest real, demoable feature: paste up to 5 URLs, generic adapter only, synchronous-feeling job (still goes through the real queue for consistency, doc 07), preview + CSV export. No auth, no adapters beyond generic, no Playwright yet. | doc 08 (generic adapter + schema, HTTP-only), doc 07 (task pipeline, simplified — one-off jobs not schedules), doc 05 §9, doc 06 §3, doc 12 §2 (first adapter fixtures) |
| **2. Accounts & workspace foundation** | Register/login (doc 04 §4.2), workspaces/members, projects/targets/schedules CRUD and plan-limit checks — but schedules don't fire yet. Proves out multi-tenancy + RLS (doc 05 §11) before any scraping logic depends on it. | doc 05 §2/§4, doc 06 §2/§4/§5, doc 03 §2 (RBAC) |
| **3. Scheduling & scraping engine (generic adapter, Mode 2)** | The scheduler/coordinator/worker pipeline for real: recurring schedules actually fire, full state machine (doc 07), domain rate limiting (doc 04 §3 layer 3), idempotency, retries/backoff, current + historical records (doc 05 §6). Generic adapter only — this phase is about the *engine*, not adapter breadth. | doc 04 §5, doc 07 (all), doc 08 §4 |
| **4. Diff engine & email alerts** | Diff computation on every task write, anomaly/suspicious-run detection (doc 10 §4), alert rules, email delivery via `NotificationProvider` (mailhog in dev). | doc 10 (all), doc 11 §5 (ops vs. user alert separation) |
| **5. Shopify & WooCommerce adapters** | Second and third adapters, proving the adapter interface actually generalizes (doc 08 §1–2/§5) rather than just describing it well. Fixture-driven (doc 12 §2) for both from the start. | doc 08 §2/§5 |
| **6. Billing & plan enforcement** | `BillingProvider`/Stripe integration, checkout/portal flows, webhook handling, usage metering wired to real quota enforcement (doc 09), plan up/downgrade lifecycle including the auto-pause-excess-targets behavior. | doc 09 (all), doc 06 §9 |
| **7. Exports & API access** | CSV/XLSX/JSON export worker, presigned-download flow, API keys + read-only REST API for Pro+/Business. | doc 06 §8/§10, doc 05 §8 |
| **8. Observability hardening & first deploy** | Prometheus/Grafana wired for real (metrics doc 11 §2 actually emitted, not just designed), load-test against the doc 00 §3 target numbers, staging VPS stood up, then production per doc 14. | doc 11, doc 14 |
| **9+. Post-launch** | Amazon/eBay/Walmart/Shopee/Lazada adapters (each needs its own ToS/anti-bot review before starting, per doc 03 — not a rubber-stamp addition), webhook/Slack/Telegram notification providers (doc 10 §6), RabbitMQ broker swap if durability needs outgrow Redis (doc 04 §4.1), Redis instance split (doc 04 §4.6), 2FA. | — |

## Why this order

Phase 1 before Phase 2/3 despite Mode 2 being the "real product": it's the fastest path to something you can look at and validate the extraction quality on, using the least amount of scaffolding (no auth, no scheduling) — and it forces the adapter/normalize/validate pipeline (doc 08) to exist and be correct before anything more complex (scheduling, diffing, billing) gets layered on top of it. Phase 3 deliberately ships with only the generic adapter so the scheduling/queue engine — the highest-risk, most novel part of the system — gets proven and tested in isolation before Phase 5 adds adapter complexity on top of a working engine. Billing (Phase 6) comes after there's something worth paying for, not before.

## Per-phase exit criteria (applies to every phase above)

- Compiles/starts via `docker compose up` with no manual steps beyond what doc 13 §3 documents.
- Has tests (unit + at least one integration test per doc 12) committed alongside the feature, not after.
- Demo script or `curl`/UI steps provided so the phase can be verified without reading the diff.
- Design docs updated in the same PR if the implementation surfaced a gap or a better approach than what's written here (doc 00 §6).

## Immediate next step

Once you approve (or amend) these 16 documents, Phase 0 starts with an exact file-by-file plan and the actual `docker-compose.yml`/Dockerfiles/CI config as real files — proposed for your review before anything is committed, per your rule of exact file paths for every change.
