# 06 — API Contract

This is the authoritative contract. Once implementation starts, FastAPI's generated OpenAPI schema must match this document; if a phase needs to deviate, this doc is updated in the same PR (doc 00 §6).

## 1. Conventions

- Base path: `/api/v1`. Versioned from day one via URL prefix — cheap now, painful to retrofit.
- **Auth:** interactive endpoints use the httpOnly-cookie JWT described in doc 04 §4.2 (`Authorization` header not required from the browser). Programmatic/API-tier endpoints accept `Authorization: Bearer sk_live_...` (API keys, doc 04 §4.2). Endpoints note which they accept; some accept both.
- **Pagination:** cursor-based on every list endpoint (`?cursor=&limit=`, default `limit=25`, max `100`). Response shape: `{"data": [...], "pagination": {"next_cursor": "...", "has_more": true}}`. Chosen over offset pagination because `runs`/`records`/`diffs` are the highest-volume, most append-heavy tables (doc 05 §5,§7) — offset pagination degrades as they grow, cursor pagination doesn't.
- **Errors:** `{"error": {"code": "STRING_CODE", "message": "human-readable, safe to show", "details": {...optional}}}`, with a matching HTTP status. Internal errors never leak stack traces or DB details; every error response also carries an `X-Request-Id` header for support/debugging correlation with logs (doc 11 §2).
- **Idempotency:** any `POST` that creates a billable or side-effecting resource (manual run trigger, export request) accepts an optional `Idempotency-Key` header; replaying the same key returns the original result instead of creating a duplicate.
- **Multi-tenancy:** every path under `/workspaces/{workspace_id}/...` checks the caller is a member of that workspace with sufficient role *before* touching any data — a 404 (not 403) is returned if the workspace exists but the caller isn't a member, to avoid confirming workspace existence to outsiders.

## 2. Auth

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /auth/register` | none | Create user + default workspace |
| `POST /auth/login` | none | Sets access/refresh cookies |
| `POST /auth/refresh` | refresh cookie | Rotates refresh token, issues new access token |
| `POST /auth/logout` | access cookie | Revokes refresh token, clears cookies |
| `POST /auth/verify-email` | none (token in body) | |
| `POST /auth/forgot-password` | none | Always 200, doesn't reveal whether email exists |
| `POST /auth/reset-password` | none (token in body) | |

## 3. Free scraper (Mode 1)

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /free/scrapes` | none (IP/fingerprint rate-limited) | Body: `{urls: string[≤5], fields: string[≤8]}`. Returns `{job_id, status}` |
| `GET /free/scrapes/{job_id}` | none, job_id acts as capability token | Poll status + results once `completed` |
| `GET /free/scrapes/{job_id}/export.csv` | none | 410 Gone once `expires_at` passed |

## 4. Workspaces & members

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /workspaces` | user | Workspaces the caller belongs to |
| `POST /workspaces` | user | Create (subject to "workspaces per user" fair-use, not a hard plan limit) |
| `GET /workspaces/{id}` | member | |
| `PATCH /workspaces/{id}` | admin+ | |
| `GET /workspaces/{id}/members` | member | |
| `POST /workspaces/{id}/members/invite` | admin+ | Body: `{email, role}` |
| `PATCH /workspaces/{id}/members/{member_id}` | admin+ | Change role |
| `DELETE /workspaces/{id}/members/{member_id}` | admin+ | |

## 5. Projects, schemas, targets, schedules

| Method & path | Auth | Purpose |
|---|---|---|
| `GET/POST /workspaces/{wid}/projects` | member / member+ | |
| `GET/PATCH/DELETE /projects/{id}` | member / admin+ | |
| `GET/POST /projects/{id}/schemas` | member / member+ | |
| `GET/POST /projects/{id}/targets` | member / member+ | Enforces `max_targets` plan limit on create |
| `POST /projects/{id}/targets/bulk-import` | member+ | CSV upload; returns per-row result (detected adapter, or rejection reason) *before* committing, so the caller can review prior to save (doc 01 J2 step 2) |
| `PATCH/DELETE /targets/{id}` | member+ / admin+ | |
| `GET/POST /projects/{id}/schedules` | member / admin+ | Enforces `min_schedule_interval_minutes` plan limit |
| `PATCH/DELETE /schedules/{id}` | admin+ | |

## 6. Runs & tasks

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /projects/{id}/runs` | member | Cursor-paginated, filterable by status |
| `POST /projects/{id}/runs` | member+ | Manual trigger; enforces quota (doc 04 §3 layer 1) |
| `GET /runs/{id}` | member | Includes `total_tasks/succeeded_tasks/failed_tasks` for progress bars |
| `GET /runs/{id}/tasks` | member | Cursor-paginated, per-target status + `error_reason` |

## 7. Records & diffs

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /projects/{id}/records` | member | Current state, filterable by target/stock_status/etc. |
| `GET /records/{id}` | member | |
| `GET /records/{id}/history` | member | `record_versions`, cursor-paginated, bounded by plan's retention window |
| `GET /projects/{id}/diffs` | member | Filterable by `diff_type`, `severity`, date range |
| `GET/POST /projects/{id}/alert-rules` | member / admin+ | |
| `PATCH/DELETE /alert-rules/{id}` | admin+ | |
| `GET /workspaces/{wid}/alerts` | member | Delivery log |

## 8. Exports

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /projects/{id}/exports` | member+ | Body: `{format: csv\|xlsx\|json}`; rejected if format not in plan's `export_formats` |
| `GET /exports/{id}` | member | Status |
| `GET /exports/{id}/download` | member | 302 to a short-lived presigned object-storage URL, never a raw storage key |

## 9. Billing

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /workspaces/{wid}/subscription` | member | Current plan + status |
| `GET /workspaces/{wid}/usage` | member | Current-period `usage_counters` vs. plan limits |
| `POST /workspaces/{wid}/checkout-session` | admin+ | Returns Stripe Checkout URL (doc 09 §1) |
| `POST /workspaces/{wid}/billing-portal-session` | admin+ | Returns Stripe-hosted portal URL |
| `POST /webhooks/stripe` | Stripe signature, not user auth | Entry point for `BillingProvider.handle_webhook_event` |

## 10. API keys (Pro+)

| Method & path | Auth | Purpose |
|---|---|---|
| `GET/POST /workspaces/{wid}/api-keys` | admin+ | POST returns the full key **once**, never again |
| `DELETE /api-keys/{id}` | admin+ | Revoke (soft — sets `revoked_at`) |

## 11. Example payloads

**`POST /projects/{id}/targets/bulk-import` response** — shows the "review before commit" pattern used throughout for bulk operations:

```json
{
  "rows": [
    {"row": 1, "url": "https://example-store.com/products/widget", "adapter_detected": "shopify", "confidence": 0.94, "status": "ready"},
    {"row": 2, "url": "https://acme.example/p/12345", "adapter_detected": "generic", "confidence": 0.6, "status": "ready"},
    {"row": 3, "url": "https://internal.example/admin", "adapter_detected": null, "status": "rejected", "reason": "dns_or_ssrf_blocked"},
    {"row": 4, "url": "https://example-store.com/products/widget", "adapter_detected": "shopify", "confidence": 0.94, "status": "duplicate_in_batch"}
  ],
  "summary": {"ready": 2, "rejected": 1, "duplicate_in_batch": 1}
}
```

**`GET /runs/{id}` response:**

```json
{
  "id": "8f2b...",
  "project_id": "a11c...",
  "status": "running",
  "triggered_by": "schedule",
  "total_tasks": 48,
  "succeeded_tasks": 31,
  "failed_tasks": 2,
  "is_suspicious": false,
  "started_at": "2026-08-23T06:00:04Z",
  "finished_at": null
}
```

**`GET /projects/{id}/diffs` item shape:**

```json
{
  "id": "d391...",
  "diff_type": "price_change",
  "severity": "warning",
  "target_id": "t882...",
  "previous_value": {"price": "49.99", "currency": "USD"},
  "new_value": {"price": "39.99", "currency": "USD"},
  "detected_at": "2026-08-23T06:02:11Z"
}
```

## 12. What's deliberately not in v1

No GraphQL (REST is sufficient at this scope and simpler to secure/rate-limit per-endpoint). No bulk write API beyond target CSV import. No public webhook *subscription management* endpoints until webhook delivery itself ships (doc 10 §5) — `alert_rules.channels` accepts `webhook` in the schema already so the DB doesn't need a migration when it lands, but the API rejects it with `code: CHANNEL_NOT_AVAILABLE` until then.
