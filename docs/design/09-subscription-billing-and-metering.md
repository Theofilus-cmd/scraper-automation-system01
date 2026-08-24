# 09 — Subscription, Billing & Usage Metering

## 1. Billing abstraction

```python
class BillingProvider(Protocol):
    def create_customer(self, workspace: Workspace) -> str: ...                       # returns provider customer id
    def create_checkout_session(self, workspace: Workspace, plan: Plan) -> str: ...    # returns redirect URL
    def create_billing_portal_session(self, workspace: Workspace) -> str: ...
    def get_subscription(self, provider_subscription_id: str) -> BillingSubscription: ...
    def cancel_subscription(self, provider_subscription_id: str, at_period_end: bool) -> None: ...
    def handle_webhook_event(self, payload: bytes, signature: str) -> list[NormalizedBillingEvent]: ...
```

`StripeBillingProvider` is the only v1 implementation, using the official `stripe` SDK. `NormalizedBillingEvent` is a small closed set our own code reacts to — `subscription.created`, `subscription.updated`, `subscription.canceled`, `invoice.paid`, `invoice.payment_failed` — deliberately not "whatever Stripe's webhook payload happens to contain," so a future second provider only needs to map its own events onto this same set.

**Why an abstraction at all, given Stripe is the only planned provider:** the actual risk it manages isn't "we might switch providers" (possible but not the main point) — it's keeping every other part of the codebase (`quota.py`, the API's plan-limit checks, the frontend's usage UI) from ever importing anything Stripe-specific. Entitlement logic is testable without a Stripe test-mode account; billing logic changes don't risk touching entitlement logic.

## 2. Entitlements vs. billing — the source-of-truth boundary

- `plans.limits` (doc 05 §3) and `subscriptions.status` in **our own Postgres** are what every quota check (doc 04 §3 layer 1) reads. This lookup is a plain indexed query, always available even if Stripe is down.
- Stripe is **never** called on the request path that checks "can this workspace do X right now." It's only called when the user explicitly initiates a billing action (checkout, portal) or asynchronously via webhook.
- `POST /webhooks/stripe` (doc 06 §9) verifies the Stripe signature, converts the event via `handle_webhook_event`, and applies it to `subscriptions`/`workspaces.plan_id` in a single transaction. Webhook processing is idempotent (Stripe event IDs are deduplicated — a `processed_webhook_events(event_id, processed_at)` table, checked before applying) since Stripe retries undelivered-ack'd webhooks.

## 3. Usage metering

- Every terminal task (doc 07 §2) increments `usage_counters` for the owning workspace: `metric='scrapes'` always, `metric='browser_scrapes'` additionally if the task ran on `queue=browser`. Incremented in the same transaction as the task's terminal-state write (no separate reconciliation needed to keep counts accurate).
- `period_start`/`period_end` align to the workspace's Stripe billing cycle (available from the `subscriptions` row), not calendar months — so usage resets exactly when billing resets.
- The coordinator's quota check (doc 04 §3 layer 1, doc 07 §6) reads the **current period's** `usage_counters` row before creating tasks for a run; a workspace at 100% blocks new scheduled/manual runs (with the per-plan overage behavior from doc 02 §3) but never retroactively fails already-running tasks.
- `GET /workspaces/{id}/usage` (doc 06 §9) is the same read, exposed to the UI for the progress-bar-per-metric view (doc 01 J4 step 2). A background check fires the 90%-threshold warning (in-app notice + one email, not repeated per request) the first time a period crosses it.

## 4. Draft plan economics — how the numbers in doc 02 §3 were derived

**This section's numbers are a starting estimate to sanity-check pricing against cost, not a researched market analysis or a promise of exact margins.** I don't have current, verified competitor-pricing data for this product category, so the price points are reasoned from cost + typical SaaS margin targets, not from market comps — validate against your own positioning before this ships.

Rough cost model at the stated MVP scale (doc 00 §3 — I'm assuming realistic steady-state load nearer the low end of that range, ~5,000 fetches/day ≈ 150,000/mo, since the target *counts* given are a capacity ceiling across all workspaces, not all simultaneously at max schedule frequency):

| Cost driver | Rough estimate | Basis |
|---|---|---|
| HTTP-only scrape | < $0.001 each | Amortized share of a single ~$60/mo VPS running the whole stack; HTTP fetches are CPU/bandwidth-light |
| Browser-rendered scrape | ~$0.002–0.005 each | Full Chromium context per fetch — roughly 15–40× the resource cost of an HTTP-only fetch |
| Storage (records/versions/exports) | Negligible at this scale | Rows are KB-scale; even high version-churn stays in the hundreds-of-MB range platform-wide |
| Transactional email | Negligible | Digest-per-run cadence stays within most providers' free/low tiers at 10–50 users |

**Sanity check at Business tier's full quota** (60,000 scrapes + 5,000 browser-rendered/mo, doc 02 §3): rough marginal cost ≈ 60,000 × $0.0005 + 5,000 × $0.003 ≈ **$45/mo**, against a $299/mo draft price → healthy gross margin even if a customer fully maxes out their quota every month.

**The honest conclusion:** at this scale, infra is close to a fixed cost shared across all workspaces on the one VPS, not a meaningful per-scrape variable cost — so price is mainly a function of *plan value* (targets, seats, retention, feature access), and the per-scrape numbers above matter more for **sizing plan limits sensibly** (so no single tenant's usage threatens the shared VPS's headroom for everyone else) than for setting the price itself. This also means margin holds up even if actual usage comes in higher than the "low thousands/day" assumption, as long as the fairness cap (doc 04 §3 layer 2) keeps any one tenant from starving the others.

## 5. Plan lifecycle

- **Upgrade:** takes effect immediately; Stripe prorates. Any plan-limit-gated feature (browser rendering, API access, export formats) is available as soon as the webhook lands (typically seconds).
- **Downgrade:** takes effect at the end of the current billing period (standard Stripe behavior via `cancel_at_period_end`-style scheduling on the subscription item). If the workspace is over the new plan's `max_targets` at that point, the **most-recently-added** targets beyond the new limit are automatically set `is_active=false` (paused, not deleted) — the UI shows exactly which targets this will affect *before* the user confirms the downgrade, so it's never a surprise.
- **Payment failure (`past_due`):** grace period (default 7 days, configurable) with an in-app banner + email on day 0 and a reminder mid-grace. If unresolved at grace-period end, the workspace moves to a restricted state — existing data stays visible/exportable, but scheduling and new runs are paused — rather than deleting anything. Reactivating payment resumes schedules automatically.
- **Cancellation:** access continues through the paid period already covered, then the workspace reverts to a no-plan state equivalent to "everything paused, data retained" for a defined window (default 30 days) before eligible for deletion — giving room to reactivate without data loss for an accidental or reconsidered cancellation.
- **Free → paid conversion:** no special migration path needed — Mode 1 has no account, so signing up and subscribing is just normal onboarding (doc 01 J2); free-mode job history isn't carried over (it's 48h-lived and workspace-independent by design, doc 05 §9).

## 6. What's explicitly out of scope for v1 billing

Usage-based (metered, pay-as-you-go) billing beyond the soft-overage handling in doc 02 §3 — flat tiers only. Annual billing/discounts — schema supports `billing_interval='year'` (doc 05 §3) so it's not blocked, just not launched with. Multiple subscriptions per workspace, or per-project billing — one subscription per workspace only.
