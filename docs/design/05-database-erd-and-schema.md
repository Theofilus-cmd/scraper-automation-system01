# 05 — Database ERD & Schema

PostgreSQL 16+. Migrations via Alembic (one migration per PR that changes schema — see doc 15 phase gating). This doc is the source of truth the migrations must match; if they diverge, this doc gets updated in the same PR.

## 1. Conventions

- Primary keys: `uuid` (`gen_random_uuid()`, via `pgcrypto`) on every table for consistency and to avoid enumerable IDs in URLs/APIs. Exception noted per-table where a `bigserial` alternative is called out for very-high-volume append-only tables, if `record_versions`/`audit_log` volume ever makes UUID index bloat a real problem — not needed at MVP scale.
- `created_at timestamptz not null default now()` on everything; `updated_at timestamptz` with an `ON UPDATE` trigger on mutable tables.
- Every tenant-scoped table carries `workspace_id`, even where it's reachable transitively through a join (e.g. `targets.workspace_id` in addition to `targets.project_id`) — this denormalization is deliberate: it's what makes both the RLS policies (§7) and the common "give me everything for workspace X" queries a single indexed lookup instead of a multi-hop join.
- Scraped prices: `numeric(12,2)` + `currency char(3)`, stored in the source's original currency — no FX normalization (doc 00 §5). Billing amounts (`plans.price_cents`) are integer cents, matching Stripe's convention, and are a completely separate concept from scraped prices.
- State-machine columns (`runs.status`, `tasks.status`) use a `CHECK` constraint over a fixed text set rather than a native Postgres `ENUM` — easier to extend without `ALTER TYPE` ceremony, at the cost of DB-level typo protection we compensate for with a shared Python `Enum` used on both the API and worker side.

## 2. Identity & tenancy

#### `users`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| email | citext unique not null | |
| password_hash | text nullable | nullable for future OAuth-only accounts |
| full_name | text | |
| is_active | boolean default true | |
| email_verified_at | timestamptz nullable | |
| created_at / updated_at | timestamptz | |

#### `workspaces`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| name | text not null | |
| slug | text unique not null | |
| owner_user_id | uuid fk users not null | |
| plan_id | uuid fk plans not null | |
| stripe_customer_id | text unique nullable | |
| created_at / updated_at | timestamptz | |

#### `workspace_members`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| workspace_id | uuid fk workspaces not null | |
| user_id | uuid fk users not null | |
| role | text check in (`owner`,`admin`,`member`,`viewer`) | |
| invited_at | timestamptz | |
| joined_at | timestamptz nullable | null until invite accepted |
| | | unique (workspace_id, user_id) |

## 3. Billing (full detail in doc 09)

#### `plans`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| code | text unique | `free`, `starter`, `pro`, `business` |
| name | text | |
| price_cents | integer | 0 for free |
| billing_interval | text check(`month`,`year`) | |
| limits | jsonb not null | `{max_projects, max_targets, max_seats, min_schedule_interval_minutes, included_scrapes_per_month, included_browser_scrapes_per_month, history_retention_days, export_formats[], alert_channels[], api_access, webhook_access, raw_artifact_storage_mb}` — **entitlements live here, in the DB, not in code** |
| is_active | boolean | |
| created_at | timestamptz | |

#### `subscriptions`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| workspace_id | uuid fk workspaces unique | one active subscription per workspace |
| plan_id | uuid fk plans | |
| status | text check(`trialing`,`active`,`past_due`,`canceled`,`incomplete`) | mirrors Stripe subscription status |
| stripe_subscription_id | text unique nullable | |
| current_period_start / current_period_end | timestamptz | |
| cancel_at_period_end | boolean default false | |
| created_at / updated_at | timestamptz | |

#### `usage_counters`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| workspace_id | uuid fk workspaces | |
| period_start / period_end | date | billing-cycle-aligned |
| metric | text check(`scrapes`,`browser_scrapes`,`api_calls`,`exports`,`storage_mb`) | |
| value | bigint default 0 | |
| | | unique (workspace_id, period_start, metric); index (workspace_id, period_start) |

## 4. Projects, targets, schedules

#### `projects`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| workspace_id | uuid fk workspaces not null | index |
| name | text | |
| description | text nullable | |
| created_by | uuid fk users | |
| created_at / updated_at | timestamptz | |

#### `extraction_schemas`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects not null | |
| name | text | |
| enabled_fields | text[] | subset of the 17 canonical fields, doc 08 §3 |
| custom_selectors | jsonb nullable | manual per-field CSS/XPath override, generic adapter only |
| is_default | boolean | |
| created_at / updated_at | timestamptz | |

#### `targets`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects not null | |
| workspace_id | uuid fk workspaces not null | denormalized, indexed |
| url | text not null | as entered by user |
| normalized_url | text not null | tracking params stripped, trailing slash/case normalized — used for dedup |
| adapter_type | text check(`generic`,`shopify`,`woocommerce`) | set at save time from detection (doc 08 §2), re-checked periodically |
| schema_id | uuid fk extraction_schemas | |
| is_active | boolean default true | paused targets keep history but stop scheduling |
| created_at / updated_at | timestamptz | |
| | | unique (project_id, normalized_url); index (workspace_id); index (project_id, is_active) |

#### `schedules`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects not null | |
| workspace_id | uuid fk workspaces not null | denormalized |
| target_id | uuid fk targets nullable | null = applies to all active targets in the project |
| interval_minutes | integer nullable | simple case: 1440 (daily), 360 (6h) |
| cron_expression | text nullable | used when interval doesn't fit (`custom`); exactly one of interval_minutes/cron_expression is set |
| timezone | text default `'UTC'` | |
| is_active | boolean default true | |
| next_run_at | timestamptz | what the scheduler polls on |
| last_run_at | timestamptz nullable | |
| created_at / updated_at | timestamptz | |
| | | **partial index** `(next_run_at) WHERE is_active` — this is the scheduler's hot query, keep it cheap |

## 5. Execution (full state machine in doc 07)

#### `runs`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects not null | |
| workspace_id | uuid fk workspaces not null | denormalized |
| schedule_id | uuid fk schedules nullable | null if manually/API triggered |
| status | text check(`pending`,`running`,`completed`,`completed_with_errors`,`failed`,`cancelled`) | |
| triggered_by | text check(`schedule`,`manual`,`api`) | |
| is_suspicious | boolean default false | doc 10 §4 anomaly flag — suppresses normal diff alerting |
| total_tasks / succeeded_tasks / failed_tasks | integer default 0 | maintained as tasks complete, avoids a COUNT(*) on every progress poll |
| started_at / finished_at | timestamptz nullable | |
| created_at | timestamptz | |
| | | index (project_id, created_at desc); index (workspace_id, created_at desc); **partial index** `(id) WHERE status IN ('pending','running')` for the reconciliation sweep (doc 04 §4.1) |

#### `tasks`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| run_id | uuid fk runs not null | |
| target_id | uuid fk targets not null | |
| workspace_id | uuid fk workspaces not null | denormalized |
| status | text check(`queued`,`in_progress`,`succeeded`,`failed`,`retrying`,`dead_letter`) | |
| idempotency_key | text unique not null | deterministic: `hash(run_id, target_id)` |
| attempt_count | integer default 0 | |
| max_attempts | integer default 3 | |
| worker_id | text nullable | for debugging which process handled it |
| http_status | integer nullable | |
| duration_ms | integer nullable | |
| error_reason | text nullable | `timeout`, `robots_disallowed`, `blocked_by_target`, `missing_required_field`, `quota_exceeded`, `network_error`, `parse_error`, `unsupported_adapter`, `dns_or_ssrf_blocked` |
| error_detail | jsonb nullable | |
| queued_at / started_at / finished_at | timestamptz nullable | |
| | | index (run_id); **partial index** `(id) WHERE status IN ('queued','retrying')`; index (target_id, finished_at desc) |

## 6. Data — current state and history

#### `records` (current snapshot, one row per product identity per target)
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| workspace_id / project_id / target_id | uuid fk | denormalized workspace_id |
| run_id | uuid fk runs | most recent run that produced this snapshot |
| product_identity_key | text not null | `sku`, else `product_id`, else normalized `product_url` — see doc 10 §1 |
| product_name | text | |
| brand | text nullable | |
| category | text nullable | |
| price | numeric(12,2) nullable | |
| currency | char(3) nullable | |
| original_price | numeric(12,2) nullable | |
| discount | numeric(6,2) nullable | percent; derived when both prices present, else source-reported |
| sku | text nullable | |
| product_id | text nullable | |
| variant | text nullable | |
| stock_status | text check(`in_stock`,`out_of_stock`,`preorder`,`unknown`) | |
| rating | numeric(3,2) nullable | |
| review_count | integer nullable | |
| description | text nullable | |
| image_url | text nullable | |
| product_url | text not null | |
| scraped_at | timestamptz not null | system-set, never source-provided |
| is_valid | boolean not null | doc 08 §4 |
| validation_errors | jsonb nullable | |
| created_at / updated_at | timestamptz | |
| | | unique (target_id, product_identity_key); index (workspace_id); index (project_id); index (target_id) |

#### `record_versions` (append-only history)
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| record_id | uuid fk records | |
| workspace_id | uuid fk | denormalized, for retention purge |
| run_id | uuid fk runs | |
| *(all fields from `records` above)* | | full snapshot at this point in time |
| change_summary | jsonb nullable | which fields changed vs. the prior version, for fast timeline rendering |
| version_created_at | timestamptz default now() | |
| | | index (record_id, version_created_at desc); index (workspace_id, version_created_at) |

Written on **change or first sighting only** (not every run) to keep growth proportional to actual change activity rather than schedule frequency — a target checked every 6h that never changes doesn't produce 4 identical rows/day. `runs.succeeded_tasks` plus `tasks` rows already give a complete "was this target checked" audit trail without duplicating full snapshots; if a future need arises for "prove we checked on schedule even with zero changes," that's a cheaper add (`record_observations(record_id, run_id, observed_at)`, no full snapshot) rather than duplicating this table's design now.

## 7. Diffs & alerts (full design in doc 10)

#### `diffs`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| workspace_id / project_id / target_id | uuid fk | |
| record_id | uuid fk records nullable | nullable — e.g. `removed_product` may reference an archived identity |
| run_id | uuid fk runs | |
| diff_type | text check(`price_change`,`stock_change`,`new_product`,`removed_product`,`data_quality_anomaly`) | |
| previous_value / new_value | jsonb nullable | |
| severity | text check(`info`,`warning`,`critical`) | |
| detected_at | timestamptz default now() | |
| | | index (project_id, detected_at desc); index (run_id) |

#### `alert_rules`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects | |
| workspace_id | uuid fk | denormalized |
| name | text | |
| trigger_types | text[] | subset of `diff_type` |
| conditions | jsonb nullable | e.g. `{"price_change_pct_gte": 3}`, `{"target_ids": [...]}` |
| channels | text[] | subset of `email`,`webhook`,`slack`,`telegram` — latter 3 rejected at creation until those providers ship (doc 10 §5) |
| is_active | boolean default true | |
| created_by | uuid fk users | |
| created_at / updated_at | timestamptz | |

#### `alerts` (delivery log)
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| alert_rule_id | uuid fk alert_rules | |
| workspace_id | uuid fk | |
| run_id | uuid fk runs | |
| diff_ids | uuid[] | diffs bundled into this one digest notification |
| channel | text | |
| status | text check(`pending`,`sent`,`failed`,`suppressed`) | |
| sent_at | timestamptz nullable | |
| error_detail | jsonb nullable | |
| created_at | timestamptz | |
| | | index (workspace_id, created_at desc) |

#### `notification_channels`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| workspace_id | uuid fk | |
| type | text check(`email`,`webhook`,`slack`,`telegram`) | |
| config | jsonb | recipient list / webhook URL+secret once enabled — secrets encrypted at rest (application-level, key from env, doc 14 §4) |
| is_verified | boolean default false | |
| is_enabled | boolean default true | forced false for webhook/slack/telegram until those ship |
| created_at / updated_at | timestamptz | |

## 8. Access & operations

#### `api_keys`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| workspace_id | uuid fk | |
| name | text | |
| key_prefix | text unique | shown in UI, e.g. `sk_live_ab12` |
| key_hash | text | hash of full key, never the key itself |
| scopes | text[] | e.g. `read:records`, `read:runs` |
| last_used_at | timestamptz nullable | |
| created_by | uuid fk users | |
| revoked_at | timestamptz nullable | |
| created_at | timestamptz | |

#### `exports`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| workspace_id | uuid fk | |
| project_id | uuid fk nullable | null = whole-workspace export |
| format | text check(`csv`,`xlsx`,`json`) | |
| status | text check(`pending`,`processing`,`completed`,`failed`) | |
| file_key | text nullable | object storage key; API returns a presigned URL, never the raw key |
| row_count | integer nullable | |
| requested_by | uuid fk users | |
| error_detail | jsonb nullable | |
| created_at / completed_at | timestamptz | |
| | | index (workspace_id, created_at desc) |

#### `audit_log`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| workspace_id | uuid fk nullable | null for system-level actions |
| actor_user_id | uuid fk users nullable | null for system-triggered |
| action | text | e.g. `member.invited`, `plan.changed`, `api_key.revoked`, `target.bulk_deleted` |
| entity_type / entity_id | text / uuid nullable | |
| metadata | jsonb nullable | |
| created_at | timestamptz | |
| | | index (workspace_id, created_at desc) |

## 9. Mode 1 — free scraper (deliberately separate, no workspace)

Kept out of the `projects`/`targets`/`schedules` machinery entirely — reusing it would make Mode 1 accidentally inherit paid-mode concepts (and risk a bug leaking scheduling/API access into the free tier). It shares only the adapter/normalize/validate pipeline in code, not these tables.

#### `free_scrape_jobs`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| client_ref | text | salted hash of IP+UA — **not the raw IP**, kept PII-light; raw IP lives only transiently in the Redis rate-limit key |
| input_urls | jsonb | |
| selected_fields | text[] | |
| status | text check(`pending`,`processing`,`completed`,`failed`) | |
| created_at | timestamptz | |
| expires_at | timestamptz | `created_at + 48h`; purge sweep deletes past this |
| | | index (expires_at) for purge; index (client_ref, created_at) as a durable backstop to the primary Redis-based rate limit |

#### `free_scrape_results`
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| job_id | uuid fk free_scrape_jobs | |
| url | text | |
| status | text check(`success`,`failed`,`duplicate`) | |
| record | jsonb nullable | |
| error_reason | text nullable | |
| created_at | timestamptz | |

## 10. Relationships summary

```
users 1───N workspace_members N───1 workspaces
workspaces 1───1 subscriptions N───1 plans
workspaces 1───N usage_counters
workspaces 1───N projects 1───N extraction_schemas
projects 1───N targets N───1 extraction_schemas
projects 1───N schedules (optionally N───1 targets)
projects 1───N runs N───1 schedules (nullable)
runs 1───N tasks N───1 targets
targets 1───N records (current) 1───N record_versions
runs 1───N diffs N───1 records (nullable)
projects 1───N alert_rules 1───N alerts N───N diffs (via diff_ids[])
workspaces 1───N notification_channels, api_keys, exports, audit_log
(free_scrape_jobs/results are workspace-independent)
```

## 11. Row-Level Security (defense in depth)

Application code always scopes queries by `workspace_id` derived from the authenticated principal — that's the primary control. RLS is added as a second, independent layer specifically because this is a multi-tenant product where a customer's competitor watch list is itself sensitive: a single missed `WHERE workspace_id = ...` in application code should not be a cross-tenant data leak.

- Every tenant-scoped table gets `ENABLE ROW LEVEL SECURITY` plus a policy of the shape `USING (workspace_id = current_setting('app.workspace_id')::uuid)`.
- The API sets `SET LOCAL app.workspace_id = '<id>'` at the start of each request's transaction, right after authenticating the principal and resolving which workspace they're acting in.
- Workers set the same session variable from the `workspace_id` denormalized onto `tasks`/`runs` before touching `records`/`diffs`/etc.
- A separate, RLS-bypassing DB role is used only for migrations and the scheduler's cross-tenant `schedules` poll (which legitimately needs to see all workspaces) — every other code path connects as the restricted role.
- This is enforced in integration tests: a fixture spins up two workspaces and asserts that workspace A's session cannot read workspace B's rows even via a deliberately-broken query missing its `WHERE` clause (doc 12 §2).

## 12. Indexing strategy summary

Every table above has `workspace_id` indexed (directly or as the leading column of a composite index) since "give me this workspace's X" is the single most common query shape. The scheduler's `schedules(next_run_at) WHERE is_active` and the reconciliation sweep's partial indexes on `runs`/`tasks` status are called out explicitly because those are polling queries that run continuously — an unindexed or non-partial version of either would degrade as the tables grow, even at this system's modest target scale.
