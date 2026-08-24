# 12 — Testing Strategy

## 1. Test layers

| Layer | Scope | Tooling |
|---|---|---|
| Unit | `domain/` (quota, diffing, validation), `scraping/normalize.py`, adapter `parse()` methods in isolation | `pytest`, no network, no DB |
| Adapter contract/fixture | Each adapter's full `matches()`→`fetch()`(mocked)→`parse()`→`normalize()`→`validate()` chain against saved real-world HTML | `pytest` + golden JSON files, §2 below |
| Integration | API endpoints against a real Postgres+Redis (docker-compose test profile or `testcontainers-python`); Celery tasks with `task_always_eager=False` against a real broker (eager mode hides real serialization/routing bugs, so it's avoided for anything queue-related) | `pytest`, `httpx.AsyncClient` against the FastAPI app, real Postgres/Redis containers |
| End-to-end | A handful of full user journeys (doc 01) against the whole Compose stack, including a local mock e-commerce page (no live third-party sites in CI) | Playwright (test driver here, separate from the product's own scraping use of Playwright) |
| Load/perf | Deferred — noted as a Phase 8+ concern (doc 15), not blocking earlier phases | Locust, when it's time |

## 2. Adapter fixtures — the pattern that keeps adapters honest

```
tests/fixtures/adapters/
  shopify/
    basic_product.html
    basic_product.expected.json
    sold_out_variant.html
    sold_out_variant.expected.json
  woocommerce/
    category_listing.html          # multi-record page
    category_listing.expected.json # a JSON array
  generic/
    jsonld_only.html
    og_tags_only.html
    heuristic_fallback.html
```

Each `*.expected.json` is the exact `NormalizedRecord` (or list, for listing pages) the adapter must produce from that fixture. The test is mechanical: load the HTML, run it through `parse()`→`normalize()`→`validate()` with network mocked out entirely, assert equality against the golden file. This is what "parser fixtures" (required by the original constraints) means concretely — it's also what makes a future markup change on a real site *safe* to investigate: pull the new HTML into a fixture, see exactly which fields the current adapter gets wrong, fix, and the fixture stays as a regression test forever. Fixture HTML is trimmed/sanitized (no need for a full multi-hundred-KB real page) but kept structurally real, not hand-written to be easy to parse.

## 3. Multi-tenancy isolation test (called out explicitly, doc 05 §11)

A dedicated integration test creates two workspaces with data, then — using a deliberately broken query missing its `WHERE workspace_id = ...` clause — asserts the RLS policy still prevents cross-workspace reads. This runs in CI on every change touching `db/models/` or `core/` RLS session setup, not just once at initial build.

## 4. SSRF hardening test (doc 03 §3)

A table-driven test asserting the fetcher rejects: `file://`/`ftp://` schemes, hostnames resolving to RFC1918/loopback/link-local ranges (including a mocked `169.254.169.254` resolution), and a redirect chain that starts public but redirects into a private range. This is treated as a security-critical test, not an edge case — it fails the build if broken, no override.

## 5. CI quality gates

All required to pass before merge, run on every PR:

| Gate | Tool | Notes |
|---|---|---|
| Lint | `ruff` (Python), `eslint` (TS) | |
| Type check | `mypy` (Python, `--strict` on `domain/` and `scraping/adapters/` at minimum), `tsc --noEmit` (TS) | |
| Dependency scan | `pip-audit`, `npm audit` | Fails on known-critical/high CVEs in direct deps |
| Secret scan | `gitleaks` | Runs on the diff, not just full history, so it's fast enough to gate every PR |
| Tests | `pytest` (backend), `vitest`/`playwright test` (frontend) | Coverage tracked, not hard-gated at a percentage in v1 — gating on a number tends to produce padding tests rather than better ones; reviewed qualitatively per PR instead |

## 6. Seed data & demo project

A `scripts/seed_demo.py` (idempotent — safe to re-run) creates: one demo user/workspace, one Pro-plan subscription (test-mode), one project ("Demo — Running Shoes") with a handful of targets pointing at the local mock e-commerce fixture site (§7) covering all three v1 adapters, a daily schedule, one alert rule, and two synthetic historical runs so the Diffs/history views have something to show immediately after `docker compose up`. This is what doc 14/15 refer to as "the stack is useful within minutes of first boot," not just "the stack starts."

## 7. Local mock e-commerce site (for E2E and adapter development)

A tiny static-ish site (own Compose service, doc 13 §1) serving a handful of Shopify-shaped, WooCommerce-shaped, and generic product pages with controllable state (a page that can be toggled in/out of stock, price changed, product removed) — this is what E2E tests and adapter development run against instead of real third-party sites, keeping CI from depending on the internet or risking hitting real targets during test runs (which would itself violate the politeness principles in doc 03).
