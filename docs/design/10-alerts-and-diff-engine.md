# 10 — Alerts & Diff Engine Design

## 1. Product identity resolution

Before anything can be "diffed," two runs' outputs for the same target have to be matched up product-by-product. Resolution order, first match wins:

1. `sku` (if present and non-empty on both sides)
2. `product_id` (platform-native ID — most reliable for Shopify/WooCommerce, since it survives name/price edits)
3. normalized `product_url` (canonical form, tracking params stripped — doc 08 §3)
4. *(only if none of the above are available, e.g. a generic-adapter target with no SKU on a listing page)* fuzzy match on normalized `product_name` + `variant` within the same target — lowest confidence, logged as such

This resolved value is `records.product_identity_key` (doc 05 §6). A target that's a **single product page** always yields one identity per run. A target that's a **listing/category page** can yield many — this distinction matters for `new_product`/`removed_product` detection below.

## 2. Diff types

| Type | Detected when | Severity |
|---|---|---|
| `price_change` | `price` or `currency` differs from the current `records` row for the same identity | `warning` if within alert rule's threshold-adjacent range, `info` for negligible fractional changes below any configured threshold, `critical` reserved for large moves (default: single-run change ≥ 30%, configurable) |
| `stock_change` | `stock_status` differs (any transition, e.g. `in_stock`→`out_of_stock`) | `warning`; `in_stock`→`out_of_stock` and back within the same day is still reported as two separate diffs, not suppressed — visibility over neatness |
| `new_product` | An identity appears for a **listing-type target** that wasn't present in that target's prior run | `info` |
| `removed_product` | An identity present in a listing-type target's prior run is absent from the current run | `warning` — but see anomaly suppression (§4), since this is the diff type most vulnerable to false positives from a broken parser |
| `data_quality_anomaly` | See §4 | `warning` or `critical` depending on severity of the anomaly |

Diffs are computed **inline**, as part of the same write that upserts a `records` row (doc 04 §5 step 4) — not a separate batch pass over the whole database — so diff detection latency is bounded by task latency, not by a periodic job's schedule.

## 3. What "removed" means for a single-product-page target

A single-product-page target (the common case) doesn't have a meaningful "removed_product" concept the same way a listing page does — there's only ever one identity. If the page itself 404s or the product is clearly gone (adapter can't find the expected product markup at all), that surfaces as a **task failure** (`error_reason` appropriate to what happened) plus, if the failure is a clean "page no longer exists" signal rather than a transient fetch problem, a `removed_product` diff against the single identity that target used to track. The distinction: a timeout or 5xx is *not* evidence the product was removed (task fails, retries per doc 07, no diff); a clean 404/410 or a parse that confidently finds "this product is discontinued" markup *is* evidence, and produces the diff.

## 4. Anomaly / suspicious-result detection

This is the mechanism behind "detect suspicious results such as an extreme drop in record count or high null rate" and "do not claim a scrape succeeded if validation fails" acting *at the run level*, not just the per-task level in doc 08 §4.

For each target, a rolling baseline is maintained from its last N successful runs (default N=10): median record count, and median null-rate across optional fields. A new run is flagged `is_suspicious` (doc 05 §5) if, relative to that baseline:

- **Record count** (for listing-type targets) drops by more than a configurable threshold (default 50%), **or**
- **Null rate** on fields that were previously reliably populated for this target exceeds a threshold (default 20%), **or**
- **100% of tasks in the run failed or dead-lettered** (doc 07 §3) — a whole-run anomaly, usually meaning the target domain is blocking us wholesale.

**Effect of the flag:** a suspicious run's diffs are still computed and stored (nothing is thrown away), but they are **excluded from normal alert-rule evaluation** — specifically, `removed_product` and `stock_change`→`out_of_stock` diffs, the two types a broken parser would most convincingly fake. Instead, a single `data_quality_anomaly` diff/alert fires: "run #N for target X looks unreliable (record count dropped from ~40 to 3) — check whether the site changed its markup before trusting these results." This is a deliberate tradeoff: a few real "the site actually removed 37 products" events will occasionally get held back for manual confirmation, in exchange for never blasting a customer with a false mass-removal alert caused by our own parser breaking. Given the product's core promise is trustworthiness (doc 01 §1), that tradeoff is the right default; the threshold is configurable per-workspace if a customer's use case has genuinely volatile catalogs.

## 5. Alert rules & delivery

`alert_rules` (doc 05 §7) evaluation, run once per completed run (not per-diff, so a run producing 15 diffs a user cares about becomes one digest, not 15 emails):

```
for rule in active_rules(project):
    matching_diffs = [d for d in run.diffs
                       if d.diff_type in rule.trigger_types
                       and satisfies(d, rule.conditions)      # e.g. price_change_pct_gte
                       and d.target_id in rule.conditions.get("target_ids", all_targets)]
    if matching_diffs:
        for channel in rule.channels:
            if not channel_enabled(workspace, channel):        # webhook/slack/telegram gated off, §6
                continue
            create_alert(rule, run, matching_diffs, channel)    # -> queue=notifications
```

`NotificationProvider` abstraction, mirroring the billing pattern (doc 09 §1) — one interface, swappable/extensible implementations:

```python
class NotificationProvider(Protocol):
    channel: ClassVar[str]
    def send(self, alert: Alert, diffs: list[Diff], destination: NotificationChannel) -> DeliveryResult: ...
```

v1 ships `EmailNotificationProvider` only — **Resend** as the initial production vendor (doc 00 §4), configured entirely through environment variables (API key, sender address) so a future provider swap touches only this one class. **MailHog** captures all outgoing mail in local development, so no real Resend credentials are needed until a phase actually sends production email — Phase 0 (doc 16) stubs this worker without wiring real sending at all. The email itself is the digest described in doc 01 J3: run identifier, per-diff summary (product, old→new value, severity), links back into the app. Delivery failures are retried with the same bounded-backoff approach as scraping tasks (doc 07 §3) and recorded in `alerts.status`/`error_detail` — an alert is never silently dropped, matching the "preserve failures, don't hide them" principle applied everywhere else in this design.

## 6. Webhook / Slack / Telegram — scaffolded, not shipped

`notification_channels.type` and `alert_rules.channels` already accept these values in the schema (doc 05 §7), and `WebhookNotificationProvider`/etc. exist as stub classes raising `NotImplementedError`, so adding real delivery later is additive (new provider class + flipping `is_enabled`), not a schema migration. The API rejects creating an alert rule that selects a not-yet-enabled channel with a clear `CHANNEL_NOT_AVAILABLE` error rather than silently accepting a rule that will never fire.
