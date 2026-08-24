# 11 — Observability & Failure Handling

## 1. Structured logging

- Every log line is JSON, written to stdout (captured by Docker's logging driver, doc 14 §3) — never to a local file the app manages itself, keeping workers stateless per the module-boundary requirement (doc 04 §2).
- Every request/task carries a correlation ID propagated through everything it touches: API requests get a `request_id` (returned as `X-Request-Id`, doc 06 §1); scraping tasks carry `run_id` + `task_id`; both are attached to every log line emitted while handling them, via a context-local logger, not passed as an explicit argument to every function.
- Log levels used deliberately: `INFO` for normal lifecycle events (run started/completed, task terminal state), `WARNING` for retried/degraded conditions (domain cooldown triggered, plan quota near limit), `ERROR` only for things that need human attention (unexpected exceptions, dead-lettered tasks past a rate threshold). Scraping failures that are *expected and handled* (robots disallowed, 404) log at `INFO` with the reason — they are not errors, they're the product working correctly.

## 2. Metrics

Prometheus-format `/metrics` endpoint on the API and on each worker type (scraped by a Prometheus container in the Compose stack, doc 13 §1; Grafana for dashboards — both optional-but-recommended per doc 00 §4 item, wired up from Phase 8 in the roadmap, not blocking earlier phases).

| Metric | Type | Why it matters |
|---|---|---|
| `tasks_total{status, adapter, queue}` | counter | Success/failure rate by adapter — the first place a Shopify markup change would show up as a spike in `failed` |
| `task_duration_seconds{queue}` | histogram | HTTP vs. browser latency separately; catches a slow target before it eats the whole timeout budget |
| `queue_depth{queue}` | gauge | Backlog per queue — the earliest signal that worker capacity needs to grow (doc 14 §7 scaling triggers) |
| `domain_rate_limit_hits_total{domain}` | counter | How often layer-3 throttling actually engages — a domain constantly at its cap is a candidate for a higher configured limit, or a sign we should slow down further |
| `domain_cooldown_active` | gauge | Domains currently in a 429/503-triggered cooldown (doc 04 §3) |
| `runs_suspicious_total{project}` | counter | Anomaly-flag rate (doc 10 §4) — a project with recurring suspicious runs likely needs its parser/selectors revisited |
| `dead_letter_total{queue}` | counter | Feeds the "whole-run dead-lettered" system alert (doc 07 §3) |
| `workspace_quota_usage_ratio{workspace_id, metric}` | gauge | Drives the 90%-warning (doc 09 §3) and is a leading indicator of infra headroom (doc 09 §4) |

## 3. Health checks

- `GET /healthz` (API): process is up, no dependency checks — used for the container's own restart policy (doc 14 §3), must never false-positive-fail due to a slow dependency.
- `GET /readyz` (API): DB (via PgBouncer) and Redis both reachable — used to gate the reverse proxy sending traffic to a starting/recovering instance.
- **Worker heartbeats:** each worker process writes `worker_heartbeat:{worker_id}` to Redis with a short TTL on a fixed interval (default 30s). A worker that stops heartbeating is both a metric (`workers_alive_total`) and, combined with the reconciliation sweep (doc 07 §5) picking up its orphaned tasks, self-healing without operator action for the common case (container restarted by its `restart: unless-stopped` policy, doc 14 §3).

## 4. Error tracking

Unhandled exceptions (API and workers) report to an error-tracking service (Sentry-compatible; self-hosted GlitchTip is a Compose-friendly option if avoiding a third-party SaaS dependency matters — noted as an option, not decided) with the same correlation IDs as the logs, so a Sentry issue links straight to the matching log lines and, for a task failure, the `tasks.error_detail` row.

## 5. Two separate alerting audiences — don't conflate them

- **User-facing alerts** (doc 10): "your competitor changed their price." Delivered per the customer's own `alert_rules`, scoped to their workspace, about *their* data.
- **Ops alerts**: "the platform itself needs attention" — dead-letter rate spike, all-tasks-failed runs (doc 07 §3), disk usage high (doc 14 §5), a worker type with zero heartbeats. These go to the operator (you), via a separate, simple channel (email to an ops address, or the same email provider — not through the customer-facing `alert_rules`/`notification_channels` tables at all, which are workspace-scoped by design). Keeping these paths structurally separate means a bug can never cause an internal ops alert to leak into a customer's inbox, or vice versa.

## 6. Failure handling — index, not a re-explanation

This system's failure handling is deliberately spread across the docs that own each failure mode, rather than centralized here, because each one needs the context of the component that produces it:

| Failure mode | Handled in |
|---|---|
| Transient scrape failure (timeout, 5xx, 429/503) | doc 07 §2–3 (retry/backoff), doc 04 §3 (domain cooldown) |
| Permanent scrape failure (404, robots, blocked, validation) | doc 07 §2, doc 08 §4 |
| Broker message lost or duplicated | doc 04 §4.1, doc 07 §4–5 |
| Worker crash mid-task | doc 07 §5 (reconciliation), §11.3 above (heartbeats) |
| Parser silently degrading (site redesign) | doc 10 §4 (anomaly detection) |
| Plan quota exceeded | doc 07 §1 (run fails fast, not silently throttled), doc 09 §3 |
| Payment failure | doc 09 §5 |
| Alert delivery failure | doc 10 §5 |

This doc's job is the cross-cutting *visibility* into all of them (logs/metrics/health/error-tracking), not re-deriving the handling logic itself.
