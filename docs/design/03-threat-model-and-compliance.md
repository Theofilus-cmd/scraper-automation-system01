# 03 — Threat Model & Compliance Boundaries

## 1. Assets to protect

| Asset | Why it matters |
|---|---|
| User credentials / sessions | Account takeover → access to a customer's competitor-intelligence data and billing. |
| Workspace data (targets, records, alert rules) | Commercially sensitive to the paying customer (their competitor watch list is itself competitive intel). |
| Billing data | We store no card numbers (Stripe-hosted), but subscription/customer IDs and invoices still matter. |
| API keys | Scoped credentials for programmatic access — leak = data exfiltration or quota abuse under someone else's cost. |
| Outbound scraper egress | The single biggest platform-specific risk: this system's core function is making the app fetch attacker-influenced URLs. |

## 2. Threats and mitigations (STRIDE-lite)

| Threat class | Concrete risk here | Mitigation |
|---|---|---|
| Spoofing | Credential stuffing / weak passwords | argon2id hashing, login rate limiting, optional TOTP 2FA (post-v1), no password in logs. |
| Tampering | A bug in a query's `WHERE` clause leaks or edits another workspace's row | App-layer scoping on every query **and** Postgres RLS as defense-in-depth (doc 05 §7) — one layer failing shouldn't mean cross-tenant compromise. |
| Repudiation | "I didn't do that" on billing/plan/member changes | `audit_log` table for sensitive actions (member add/remove, plan change, API key create/revoke, target bulk-delete). |
| Information disclosure | Cross-tenant data leakage; verbose error messages leaking internals; SSRF reading internal services (below) | RLS, generic external error messages with internal correlation IDs, SSRF hardening. |
| Denial of service | Free tier abused as a scraping proxy; one tenant's backlog starves others; a target domain gets hammered | IP/fingerprint rate limits on free mode, per-workspace fairness concurrency cap, per-domain concurrency/rate cap (doc 04 §3). |
| Elevation of privilege | A `member`/`viewer` role performing `owner`-only actions via direct API calls | Server-side RBAC checked per-endpoint, never inferred from UI state. |

## 3. SSRF — the risk specific to a scraping product

The application's job is to fetch URLs a user gives it. That is, by definition, an SSRF surface unless explicitly hardened. This is treated as a first-class security control, not an afterthought:

- **Scheme allowlist:** only `http://` and `https://`. Reject `file://`, `ftp://`, `gopher://`, etc. at input validation, before any fetch is attempted.
- **DNS resolution check at fetch time, not just URL-parse time:** resolve the hostname, then reject if the resolved IP is in a private/reserved range (RFC1918 `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, loopback `127.0.0.0/8`, link-local `169.254.0.0/16` — **which includes the `169.254.169.254` cloud metadata endpoint** — and IPv6 equivalents). Checking only the literal hostname string is insufficient; a public hostname can resolve to a private IP (DNS rebinding), so this check must re-run on the resolved IP the HTTP client actually connects to, not just once upfront.
- **No redirect trust:** each redirect hop is re-validated against the same scheme/IP checks before being followed; cap redirect depth (e.g. 5).
- **Egress isolation:** scraper workers run as their own containers with no access to internal service ports/hostnames (Postgres, Redis, internal admin endpoints aren't reachable from the worker's network namespace by name or by the private IP check above).
- **Outbound timeouts everywhere:** connect timeout, read timeout, total timeout — an attacker-controlled endpoint that hangs forever must not tie up a worker indefinitely.
- Implementation detail lands in doc 04 §5 (fetcher component) and doc 08 (adapter `fetch()` contract must go through the hardened fetcher, not raw httpx/Playwright calls).

## 4. Scraping conduct boundaries (compliance-by-design, not just legal CYA)

These are hard defaults, not per-customer toggles:

- **robots.txt is honored by default**, per target domain, cached with a sane TTL. If `Disallow` covers the target path, the task fails fast with reason `robots_disallowed` — it is not retried, and it is not silently skipped as if it succeeded. There is no "ignore robots for this target" switch in v1; if a real customer need for it emerges later, it needs a deliberate, reviewed exception process — not a checkbox.
- **`Crawl-delay` is honored** as a floor on that domain's fetch interval, in addition to our own per-domain rate cap (doc 04 §3).
- **A clear, honest User-Agent** identifying the bot and linking to an info/contact page (e.g. `CompetitorWatchBot/1.0 (+https://<domain>/bot)`), sent on every request, including Playwright-rendered ones. We do not spoof a generic desktop-browser UA to evade detection.
- **CAPTCHA / active anti-bot response is a stop signal, not a puzzle to solve.** If a target starts returning CAPTCHA challenges or clear bot-block pages, the task fails with reason `blocked_by_target` and is surfaced to the user as such (with guidance to check the source's ToS/rate limits) — the system does not attempt to solve, bypass, or evade this.
- **No login-walled or paywalled content.** Adapters only operate on pages reachable without authentication.
- **Politeness independent of legality:** rate limits and Crawl-delay are honored even where a jurisdiction's case law might permit more aggressive crawling of public data — this product's reputation depends on target sites not needing to block us.

## 5. Data minimization & PII posture

- The 17-field extraction schema (doc 08 §3) is product-centric, not person-centric — it does not extract reviewer names, buyer info, or any personal data from target pages by design. If a future adapter's raw HTML incidentally contains such text in an unrelated field, normalization does not pass it through unmapped.
- Raw HTML/screenshot artifacts are **not** stored for successful scrapes. A bounded sample is stored only for *failed/anomalous* tasks, for debugging parser breakage, with a 14-day TTL and workspace-scoped access.
- Our own users' PII (email, name) and billing metadata (Stripe customer/subscription IDs — never card numbers) are encrypted in transit (TLS) and at rest (managed Postgres disk encryption or LUKS on the VPS volume, doc 14 §2).
- Account data export and deletion-on-request are supported from v1 (cheap to build in now, and this is the kind of thing that's expensive to retrofit) — this is a reasonable-privacy-practice choice, not a claim of specific regulatory certification.

## 6. Legal note (not legal advice)

Scraping-legality analysis varies by jurisdiction, target ToS, and what's actually extracted (public product data, as here, sits in a different risk posture than personal data or paywalled content — but this varies and changes over time). This system is a tool operated by you against sources you choose; the engineering controls above (robots.txt compliance, no anti-bot bypass, rate limiting, public-pages-only) reduce risk but don't eliminate the need for **your own Terms of Service / Acceptable Use Policy reviewed by a lawyer**, governing what your customers are permitted to point this system at. That drafting is out of scope for this design doc set — flagging it now so it isn't forgotten before public launch.

## 7. Explicit non-goals (repeated because they bound the threat model)

No credential theft. No private-data extraction. No CAPTCHA bypass. No unauthorized access. These aren't features we're deferring — they're things this system will not do, on any plan, regardless of what a customer requests.
