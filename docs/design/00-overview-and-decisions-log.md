# Scraper Automation System — Design Overview & Decision Log

Status: **DRAFT — pending your approval.** Nothing in `docs/design/` has been implemented yet. This document is the index into the other 15 design docs and the running record of decisions, so nothing agreed in conversation gets lost or re-litigated later.

## 1. What this system is

A two-mode product data extraction platform:

- **Mode 1 — Free one-time scraper.** Anonymous, no account. Scrape a handful of public URLs once, preview, export CSV. No history, no scheduling, no API.
- **Mode 2 — Paid continuous monitoring.** Authenticated, multi-tenant. Workspaces track target URLs on a schedule, diff results run-over-run, alert on price/stock/catalog changes, and expose exports/API per plan.

**Positioning:** not a generic "scrape any site" tool. v1 is scoped to public e-commerce product pages and competitor price/stock monitoring, built on a small set of first-class adapters (Generic, Shopify, WooCommerce) rather than one large parser. Amazon/eBay/Walmart/Shopee/Lazada adapters are explicitly deferred — those sites have aggressive anti-automation postures and their own ToS considerations that deserve dedicated review before we build against them.

## 2. Document map

| # | Document | Covers |
|---|---|---|
| 00 | This file | Decisions log, open items, assumptions |
| 01 | `01-product-vision-and-journeys.md` | Vision, positioning, user journeys |
| 02 | `02-requirements-and-feature-matrix.md` | Functional/non-functional requirements, Free vs paid feature matrix |
| 03 | `03-threat-model-and-compliance.md` | Threat model, SSRF/robots/legal boundaries, data handling |
| 04 | `04-system-architecture.md` | Component architecture, module boundaries, key tech decisions |
| 05 | `05-database-erd-and-schema.md` | Full Postgres schema, indexes, multi-tenancy isolation |
| 06 | `06-api-contract.md` | REST endpoint catalog, auth, pagination/error conventions |
| 07 | `07-queue-and-job-state-machine.md` | Scheduler→coordinator→worker flow, run/task state machines |
| 08 | `08-scraper-adapters-and-data-schema.md` | Adapter interface, detection strategy, 17-field schema, validation |
| 09 | `09-subscription-billing-and-metering.md` | Billing abstraction, draft plan pricing, usage metering |
| 10 | `10-alerts-and-diff-engine.md` | Diff types, anomaly detection, alert rules, notification delivery |
| 11 | `11-observability-and-failure-handling.md` | Logging, metrics, health checks, failure handling |
| 12 | `12-testing-strategy.md` | Test layers, fixtures, CI quality gates |
| 13 | `13-docker-compose-dev-setup.md` | Local dev Compose stack |
| 14 | `14-deployment-architecture.md` | Single-VPS production topology, backups, rollback, scaling path |
| 15 | `15-implementation-roadmap.md` | Phased vertical slices |
| 16 | `16-phase-0-walking-skeleton-plan.md` | Phase 0 file-by-file plan, prerequisites, commands, acceptance checklist |

## 3. Decisions locked in with you (2026-08-23)

| Area | Decision | Detail |
|---|---|---|
| Production deployment | Single VPS + Docker Compose | Reverse proxy w/ HTTPS, all services containerized, stateless where possible, explicit non-goal: do not design around Kubernetes for v1. Full spec in doc 14. |
| Migration path | Keep it open | API/workers stateless, durable queue, no filesystem coupling → future move to managed DB/object storage/Kubernetes doesn't require a rewrite. |
| Initial scale target | ~10–50 registered users, ~5–15 concurrent, ~100–500 active projects, ~1,000–10,000 targets, mostly daily/6h schedules, low thousands of fetches/day | Drives worker pool sizing, DB indexing, and rate-limit defaults in docs 04/05/07. All numbers are configurable, not hardcoded. |
| Rate limiting | Three independent layers | (1) plan quota per workspace/month, (2) fairness concurrency cap per workspace/project, (3) domain politeness cap (2–5 concurrent requests/domain, default 3, honors `Retry-After` on 429/503). Full design in doc 04/07. |
| Worker topology | Separate HTTP and browser-render pools | Start: 2 HTTP worker processes (concurrency 4–8 each), 1 browser worker (concurrency 2). All tunable via env. |
| Queue tech | Celery + Redis broker | Chosen over RQ/Dramatiq/Arq/Temporal — see doc 04 §4 for the full comparison. Redis-as-broker's durability gap is closed with idempotent tasks + a reconciliation sweep, not by adding RabbitMQ yet. |
| DB connection handling | PgBouncer (transaction pooling) added to the stack | Needed once API (multiple Uvicorn workers) + multiple Celery workers all hit Postgres directly — cheap to add now, expensive to retrofit later. |
| Pricing | Propose a draft | Usage-based limits (targets, scrapes/mo, browser-rendered scrapes/mo, retention, seats, export/API access), grounded in a rough cost-per-scrape estimate. **These numbers are placeholders for your review, not researched market pricing.** See doc 09. |
| Entitlements vs billing | Separated | `plans`/`subscriptions` tables are the source of truth for what a workspace can do; Stripe is synced in via webhooks through a `BillingProvider` abstraction, never queried live on the request path. |
| Local dev resource profile | Minimal default (10 services) + opt-in `browser`/`workers`/`storage`/`monitoring` Compose profiles | Added 2026-08-24 for hardware-constrained local dev (doc 16 §D). Production unaffected — its `.env` activates every profile (doc 14 §1). |

## 4. Open items from v1 of this doc — resolved 2026-08-23

All five were reviewed and decided without needing structural changes to the docs — all five were already designed as configuration, not hardcoded architecture. Doc 16 is the Phase 0 plan that followed.

1. **Pricing numbers** (doc 09) — ✅ approved as a **draft only**. Prices/limits/feature flags remain DB-configurable (`plans.limits` jsonb, doc 05 §3), explicitly not final market pricing.
2. **Reverse proxy** — ✅ **Caddy** confirmed for the initial VPS deployment (doc 14 §2).
3. **Staging environment** — ✅ **deferred.** No separate staging VPS for now; local Docker Compose is the dev/test environment. Steps to add a staging VPS later are documented in doc 14 §7.
4. **Transactional email provider** — ✅ **Resend**, configured only via environment variables through the `NotificationProvider` abstraction (doc 10 §5); MailHog for local dev. No real Resend credentials needed until a phase actually sends real email (not Phase 0).
5. **Free-tier field split** — ✅ approved exactly as proposed (doc 02 §3): `product_name, brand, price, currency, stock_status, sku, image_url, product_url`, with `scraped_at` as system metadata outside the field cap.

Also reconfirmed at this checkpoint: v1 stays limited to the generic/Shopify/WooCommerce adapters (doc 01 §4, doc 03 §7, doc 15) — Amazon/eBay/Walmart/Shopee/Lazada, login-based scraping, CAPTCHA bypass, and private-data extraction are out of scope for the initial implementation, not just deferred in the abstract.

## 5. Assumptions carried through all docs

- Single region/VPS location to start (pick nearest your primary users).
- English-only UI for v1.
- Web-responsive only, no native mobile app.
- Prices scraped are stored in their original currency (no FX normalization in v1).
- "Production-oriented" means production-quality engineering practice at MVP scale, not day-one hyperscale — matches your scale answer above.

## 6. How to use this doc set

Read in order 00 → 15 for a first pass. After that, each doc stands alone as the reference for its area. When we start implementation (doc 15), each phase should only need to touch the doc(s) relevant to that vertical slice — if a phase's code and its design doc disagree, the design doc is updated in the same PR, not silently drifted from.
