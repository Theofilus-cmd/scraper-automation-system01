# 01 — Product Vision & User Journeys

## 1. Vision

Give e-commerce teams (brand owners, marketplace sellers, category managers, pricing analysts) a trustworthy, low-effort way to know when a competitor's or partner's product page changes — price, stock, catalog composition — without writing or maintaining their own scrapers.

"Trustworthy" is the operative word: the system must never report a change it isn't confident actually happened. A broken parser or a site redesign should surface as a *data-quality alert to us/them*, never as a false "price dropped 90%" or "12 products removed" alert. Reliability and honesty about failure are treated as product features, not just engineering hygiene (see doc 03, doc 10 §4).

## 2. Positioning

- **Not** a general-purpose "point at any site" scraper. The product's promise only holds for the sources it has a real adapter for.
- v1 sources: generic public product pages (schema.org/OpenGraph-driven), Shopify storefronts, WooCommerce storefronts.
- v1 use case: **competitor and market monitoring** for e-commerce — "tell me when they change price, go out of stock, or add/remove products."
- Free mode exists to let a prospect experience the extraction quality with zero commitment, and to fill inbound top-of-funnel; it is intentionally capability-limited (no history, no automation) so it can't substitute for the paid product.

## 3. User journeys

### J1 — Anonymous visitor runs a free one-time scrape

**Actor:** unauthenticated visitor. **Goal:** "does this tool actually extract what I need, right now, from a page I care about?"

1. Visitor lands on the free-scraper page, pastes 1–5 product URLs (or uploads a small CSV).
2. Selects which of the available fields to extract (capped subset for free tier — see doc 02 §3).
3. Submits. UI shows a running job status (queued → processing → done) polling the job endpoint.
4. System scrapes each URL once, through the same adapter/normalize/validate pipeline paid mode uses.
5. Visitor sees a results preview table plus a summary strip: N succeeded, N failed (with reasons), N duplicates, N records with missing optional fields.
6. Visitor exports CSV. Job and its data are purged after 48h (no account, nothing durable).
7. If a URL is from a domain the system already has a strong Shopify/WooCommerce adapter for, the results are visibly higher-quality (more fields populated) than a site only the generic adapter can handle — this is the upsell moment, surfaced as a plain-language note, not a dark pattern.

**Success:** visitor trusts the extraction enough to consider paying for the monitored version of the same URLs.

### J2 — New paid customer sets up a monitored project

**Actor:** authenticated workspace owner on a paid plan (post-signup/checkout). **Goal:** "watch my top 20 competitor SKUs and tell me when anything changes."

1. Creates a workspace (or is in the default one created at signup), creates a Project ("Competitor watch — Q3").
2. Adds targets: pastes/imports up to their plan's target limit; system auto-detects adapter per URL (Shopify/WooCommerce/generic) and shows the detection result before saving.
3. Chooses an extraction schema (default = all applicable fields for the detected adapters) — optionally customizes which fields to track.
4. Sets a schedule (daily / every 6h / custom cron, bounded by plan's minimum interval) — see doc 07.
5. Triggers a manual "run now" to validate everything before waiting on the schedule; watches run progress (per-target status) live.
6. Reviews the first run's records; sets up an alert rule ("notify me by email on any price change ≥ 3% or stock status change").
7. Leaves. The schedule now runs unattended; owner receives their first digest alert email when something changes.

**Success:** first scheduled run completes without the owner needing to touch anything, and the alert email is specific and actionable (which product, what changed, from what to what, link to source).

### J3 — Existing customer receives and acts on a price-change alert

**Actor:** workspace member (may not be the owner). **Goal:** react fast to a competitor's move.

1. Receives a digest email: "3 changes detected in *Competitor watch — Q3* (run #482, 2026-08-23 06:00)" listing the changed products with old→new values and severity.
2. Clicks through into the app, lands on the project's Diffs view filtered to that run.
3. Inspects one product's full history (record_versions) as a timeline: price over time, stock status over time.
4. Exports the affected records (or the whole project) to XLSX for a pricing meeting.
5. Optionally tightens the alert rule threshold if the alert felt noisy, or mutes a specific target.

**Success:** the member never had to go find this information themselves — it arrived, was specific, and the historical context was one click away.

### J4 — Workspace admin manages team and billing

**Actor:** workspace owner/admin. **Goal:** add teammates, understand usage against plan, avoid surprise overage.

1. Opens workspace settings → Members, invites a teammate by email with a role (admin/member/viewer).
2. Opens Usage tab: sees current-period scrapes used vs. included, browser-rendered scrapes used vs. included, targets used vs. limit, with a plain progress bar per metric (see doc 09 §3).
3. Gets an in-app + email warning at 90% of any quota before anything is throttled.
4. If near the target-count limit, either removes stale targets or upgrades plan via a Stripe Checkout session (redirect out, redirect back), subscription status updates via webhook within seconds.
5. Views invoices/receipts (Stripe-hosted portal, linked, not rebuilt in-app for v1).

**Success:** the admin always understands where they stand against their plan before hitting a hard limit, and upgrading is a two-click flow.

## 4. Explicit non-goals for v1 (both modes)

- No arbitrary/generic site support beyond the three v1 adapters — unsupported domains fail fast with a clear "no adapter available" reason, not a best-effort guess.
- No scraping behind login walls, paywalls, or CAPTCHAs, and no bypass of anti-bot measures — see doc 03.
- No price/currency normalization to a common currency.
- No mobile app, no non-English UI.
- No webhook/Slack/Telegram alert delivery in v1 (scaffolded, disabled) — email only ships first.
