# 02 — Functional & Non-Functional Requirements, Feature Matrix

## 1. Functional requirements

### 1.1 Mode 1 — Free one-time scraper

| ID | Requirement |
|---|---|
| F1.1 | Accept 1–5 public URLs via manual entry or CSV upload (same cap either way). |
| F1.2 | User selects extraction fields from a capped subset of the 17 canonical fields (see §3). |
| F1.3 | System scrapes each URL exactly once through the shared adapter/normalize/validate pipeline. |
| F1.4 | Results preview shows per-URL status: success / failed (with reason) / duplicate. |
| F1.5 | Summary counts: succeeded, failed, duplicates, records with missing optional fields. |
| F1.6 | CSV export of results. |
| F1.7 | No scheduling, history, alerts, API access, or recurring automation (hard product boundary, not just a UI omission). |
| F1.8 | Job data purged 48h after creation. Abuse control: rate-limited per IP/fingerprint (3 jobs / rolling 24h to start). |

### 1.2 Mode 2 — Paid continuous monitoring

| ID | Requirement |
|---|---|
| F2.1 | Authenticated users create/join workspaces; workspaces contain projects. |
| F2.2 | Projects contain targets (URLs) and one or more extraction schemas. |
| F2.3 | Targets support bulk CSV import with per-row adapter auto-detection and validation feedback before save. |
| F2.4 | Schedules configurable per target or per project-default: daily, every 6h, or custom cron, bounded by plan minimum interval. |
| F2.5 | Scheduler creates runs on schedule; users can also trigger a manual run. |
| F2.6 | Coordinator expands a run into one task per active target and enqueues them durably. |
| F2.7 | Workers process tasks with timeouts, bounded retries with exponential backoff + jitter, domain-level concurrency/rate limits, and idempotent writes. |
| F2.8 | Records are normalized to the canonical schema and validated before being considered "current." |
| F2.9 | Current record state and full historical versions are both queryable. |
| F2.10 | Diff engine compares each run's output to prior state and classifies: price change, stock change, new product, removed product, data-quality anomaly. |
| F2.11 | Suspicious runs (record-count collapse, high null-rate) are flagged and excluded from normal diff alerting — see doc 10 §4. |
| F2.12 | Users configure alert rules (trigger types, thresholds, scope) per project; alerts delivered by email in v1 (webhook/Slack/Telegram scaffolded, disabled). |
| F2.13 | Paid users export CSV/XLSX/JSON per plan; higher plans get read-only API and webhook access. |
| F2.14 | Every workspace-scoped read/write is isolated to that workspace — enforced at the app layer and defense-in-depth at the DB layer (RLS, doc 05 §7). |
| F2.15 | Plan limits, usage metering, and subscription status are enforced before actions that would exceed them (creating a target, running ahead of schedule, exporting, calling the API). |
| F2.16 | Job progress (per-run, per-task) and run history are exposed in the API/UI in near-real-time. |

## 2. Non-functional requirements

| Category | Requirement |
|---|---|
| Performance | Free-mode job of 5 URLs completes p95 < 30s for HTTP-only adapters. A scheduled run's per-task p95 latency < 15s (HTTP) / < 45s (browser-rendered). |
| Availability | API target 99.5% monthly (single-VPS v1 — see doc 14 for the honest ceiling this implies, and the upgrade path). Scheduled runs may be delayed under load but must never be silently dropped. |
| Scalability | Design supports the stated target (10–50 users, 1,000–10,000 targets, low-thousands fetches/day, doc 00 §3) on one modest VPS; every pool size (worker concurrency, DB connections, per-domain concurrency) is env-configurable, not hardcoded, so growth is a config change first, an architecture change second. |
| Security | No secrets in code or images; TLS everywhere in prod; passwords hashed with argon2id; API keys hashed at rest; SSRF-hardened outbound fetcher (doc 03 §3). |
| Data integrity | A task is never marked `succeeded` if required-field validation fails (doc 08 §4). Failed URLs and their error reasons are always preserved, never silently dropped. |
| Data retention | Free-mode data: 48h. Paid-mode historical versions: per plan (doc 02 §3). Raw HTML artifacts (failure-sample only): 14 days. |
| Observability | Every run/task carries a correlation ID traceable through logs; queue depth, task success/failure rate, and per-domain throttling are all metrics (doc 11). |
| Maintainability | No platform-specific parsing logic outside its adapter (doc 08 §1); web/API layer has no scraping code in-process (doc 04 §2). |
| Compliance | Only public pages; robots.txt honored by default with no per-target bypass; no CAPTCHA/anti-bot bypass; data minimization (doc 03). |

## 3. Free vs. paid feature matrix (draft)

Full pricing rationale and the underlying cost model are in doc 09 — this table is the feature/limit summary. **All numbers below are a first draft for your review, not final.**

| Dimension | Free (Mode 1) | Starter | Pro | Business |
|---|---|---|---|---|
| Auth required | No | Yes | Yes | Yes |
| Monitored targets | n/a (one-time, ≤5/job) | 25 | 150 | 750 (custom above) |
| Projects | n/a | 3 | 10 | Unlimited |
| Team seats | n/a | 1 | 5 | 15 |
| Extraction fields | 8 of 17 (below) | All 17 | All 17 | All 17 |
| Min. schedule interval | n/a | Daily | 6 hours | 1 hour / custom cron |
| Included scrapes/mo¹ | 3 jobs/day (rate limit, not metered) | 1,000 | 15,000 | 60,000 |
| Included browser-rendered scrapes/mo | 0 | 0 | 1,000 | 5,000 |
| Overage behavior | n/a | Hard stop at 100%, resumes next cycle | Metered overage up to 25% | Metered overage |
| History retention | 48 hours | 30 days | 90 days | 180 days |
| Export formats | CSV | CSV | CSV, XLSX, JSON | CSV, XLSX, JSON |
| Alerts | n/a | Email | Email, webhook | Email, webhook, (Slack/Telegram when shipped) |
| API access | No | No | Read-only REST | Read-only REST, higher rate limit |
| Raw artifact storage | n/a | 100 MB | 1 GB | 5 GB |
| Price (draft) | $0 | $29/mo | $99/mo | $299/mo |

¹ "Scrape" = one task execution (one target fetched once), the billing unit — distinct from a "run" (one schedule firing, which contains many tasks). See doc 07 §1 for the run/task distinction and doc 09 §2 for how this is metered.

**Free-tier field split (8 of 17, proposed):** `product_name, brand, price, currency, stock_status, sku, image_url, product_url`. Paid unlocks: `category, original_price, discount, product_id, variant, rating, review_count, description`. `scraped_at` is system metadata attached to every record regardless of plan and doesn't count against the field cap.

## 4. Explicit product boundaries (repeated from constraints, because they're load-bearing)

- Only public pages, customer-owned sites, or authorized/API-based sources.
- No credential theft, private-data extraction, CAPTCHA bypass, or unauthorized access — ever, on any plan.
- A scrape is never reported as succeeded if validation failed.
- Failed URLs and their error reasons are always preserved and shown, never hidden.
