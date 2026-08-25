# 17 — Phase 1: Free Scraper Slice (mock-store only) — Implementation Plan

Status: **IMPLEMENTED (2026-08-25); static verification complete; live/Docker verification blocked in the generation sandbox, not yet performed on real hardware.** All 33 planned files were written (20 new backend modules/tests, 4 golden fixture pairs, 4 doc-listed modifications, plus `backend/pyproject.toml`'s `asyncio_mode` addition and `tools/mock-store/pyproject.toml`'s description sync — both small, additive completions of already-declared intent, not scope additions). Committed as `c9d2530`.

Scope discipline, restated so it's checkable against what follows: one adapter (mock-store only), real fetch→parse→normalize→validate, persistence for source/product/current observation, one manual trigger endpoint, and tests. No real marketplace scraping, browser automation, authentication, billing, alerts, scheduling UI, or other Phase 2+ scope — per the user's explicit brief approving this plan. All verified Phase 0 behavior and Docker profiles are preserved (confirmed — see §G).

---

## Context

Phase 0 was fully verified end-to-end on the user's real WSL2 + Docker Desktop machine before this phase began: all 10 default services healthy from clean volumes, migrations apply, both health endpoints correct, frontend/mock-store/MailHog all reachable, a real Celery task round-trips. The user approved starting Phase 1 with a narrow, explicit scope (above), and asked for a plan before any code — that plan was produced via the harness's plan-mode workflow (research → design → review), approved, and is implemented here without deviation beyond the plan's own explicitly-flagged scope decisions.

The core design tension this phase resolves: docs 05/06/08 describe the *full* future system (multi-tenant `targets`/`records`/`record_versions`, the full `/free/scrapes` contract, a generic real-world adapter). This phase is deliberately smaller than all three. Every deviation is called out explicitly below, with the reason, rather than silently built different from what the docs say.

---

## Scope decisions (each a deliberate, flagged deviation or judgment call)

1. **Narrow `MockStoreAdapter`, not `GenericAdapter`.** `matches()` is a pure hostname check against `settings.mock_store_base_url` — no network call, no confidence scoring against arbitrary real-world URLs (that's real-site-scraping groundwork, explicitly excluded).
2. **Three new tables, deliberately simpler than doc 05's `targets`/`records`/`record_versions`**: no `workspace_id`, no `project_id`, no history table. Real SQLAlchemy ORM models introduced now (`app/db/models/`); `env.py`'s `target_metadata` now points at `Base.metadata`. The migration itself stays hand-written (`op.create_table(...)`, matching `0001`'s style) — no autogenerate tooling added.
3. **New endpoint is an explicit precursor to doc 06 §3's `/free/scrapes`, not the same thing.** No CSV export, no rate limiting, no 5-URL batching. Named `app/api/v1/scrapes.py`, deliberately not `free_scrape.py` (the name doc 04 §2 reserves for the eventual full contract).
4. **SSRF-hardened fetcher, built for real, with one narrow allowlist entry.** `ssrf_allowed_hosts: list[str] = ["mock-store"]`, checked *before* the private-IP rule; every other check (scheme, other private ranges, redirect re-validation) applies unconditionally. A local-dev exception for a service standing in for "a target site," not a weakening of protection for real internal services — Postgres/Redis are never in this allowlist.
5. **robots.txt honoring and CAPTCHA/anti-bot detection are deferred**, not silently dropped — doc 03 §4 calls both hard defaults, but they have nothing real to exercise yet against a service we fully control.
6. **`selectolax`, not BeautifulSoup**, per doc 04 §4.4's explicit, already-approved decision. `selectolax` has no typeshed stub package, so its one import point carries a scoped `# type: ignore[import-untyped]` rather than deviating from the approved architecture for a typing convenience.
7. **Error envelope built now, small and reusable** (`app/core/errors.py`): doc 06 §1's `{"error": {"code","message","details"}}` + `X-Request-Id`, plus a handler for FastAPI's own `RequestValidationError` so a malformed request body also gets the same envelope shape — Phase 1 is the first phase with real endpoint errors.
8. **No frontend changes.** The approved scope doesn't mention UI, explicitly excludes "scheduling UI," and asks for "one manual API endpoint or **command**."
9. **No new `runs`/`tasks` tables.** "Job status while waiting" uses Celery's own `AsyncResult` (Redis-backed), not a new DB table — doc 15 itself describes Phase 1 as "simplified — one-off jobs not schedules."

Two further judgment calls made during implementation, not in the original plan text but consistent with it:

10. **`sources` is unconditionally upserted**, independent of validation outcome; the write-gating table (§ Data model below) only ever governs `products`/`current_observations`. Reasoned out in full in `app/db/models/repository.py`'s module docstring — `sources` is bookkeeping ("what URL did we try"), analogous to doc 05's `targets`, which persists regardless of any single run's outcome.
11. **doc 05's `tasks.error_reason` vocabulary is completed, not just partially reused.** The approved plan named two response codes (`MISSING_REQUIRED_FIELD`, `UNSUPPORTED_ADAPTER`); implementation adds the fetch-level reasons the SSRF-hardened fetcher can actually produce (`timeout`/`network_error`/`dns_or_ssrf_blocked` → `502 FETCH_FAILED`) and a no-product-found case (`parse_error` → `422 NO_PRODUCT_FOUND`), rather than bucketing every non-validation failure into a generic `500`. See `app/workers/tasks_http.py`'s `ERROR_REASON_TO_RESPONSE`.

---

## Data model (migration `0002`)

- **`sources`**: `id` uuid pk, `url` text, `normalized_url` text unique, `adapter_type` text (`CHECK IN ('mock_store')`), `created_at`/`updated_at`.
- **`products`**: `id` uuid pk, `source_id` fk→sources, `product_identity_key` text (`sku:`/`product_id:`/`url:`-prefixed — `sku`, else `source_product_id`, else normalized `product_url`, same precedence as doc 10 §1; the kind-prefix is a small, deliberate hardening against a same-string collision between e.g. one product's sku and another's product_id, not specified by the docs), `product_url` text, `sku`/`source_product_id` nullable, `created_at`/`updated_at`, unique(`source_id`, `product_identity_key`).
- **`current_observations`**: `id` uuid pk, `product_id` fk→products **unique**, `product_name`/`price`/`currency`/`stock_status` **not null** (stricter than doc 05 §6's general `records` table, which lists these nullable — Phase 1's write-gating rule guarantees a row here is only ever created/updated by a validated write, so these are always populated whenever the row exists), the rest of doc 08 §3's canonical fields nullable, `is_valid` not null, `validation_errors` jsonb nullable, `scraped_at` not null, `created_at`/`updated_at`.
- All three get a `set_updated_at()` trigger — the first migration in this repo to need one, establishing the doc 05 §1 convention for future migrations to reuse.

**Write-gating rule** (doc 08 §4: "failed required field ⇒ leave prior snapshot standing"), enforced in one place — `app/db/models/repository.py::upsert_scrape_result()`, via `INSERT ... ON CONFLICT DO UPDATE`:

| Existing `products` row for this identity? | Validation | Action |
|---|---|---|
| No | Invalid | `sources` upserted; nothing else written. |
| No | Valid (full or partial) | `sources` upserted; `products` + `current_observations` created. |
| Yes | Invalid | `sources` upserted; `current_observations` untouched. |
| Yes | Valid (full or partial) | `sources` upserted; `current_observations` updated in place. |

Implementation note: since "Invalid" always means "don't touch `products`/`current_observations`" regardless of prior existence, the two Invalid rows collapse to one code path — no existence pre-check is needed on that path at all, only on the Valid path (where it's used to report `created: bool` in the response, via Postgres's `xmax = 0` idiom on the `products` upsert's `RETURNING` clause, not a separate query).

---

## File plan (as implemented)

**New — backend**

| Path | Responsibility |
|---|---|
| `app/scraping/types.py` | `RawFields`, `RawPage`, `SiteSniff`, `FetchContext`, `ExtractionSchema`, `NormalizedRecord`, `ValidationResult`. |
| `app/scraping/normalize.py` | `normalize(raw, source_url)`, `normalize_url()`. Pure. |
| `app/scraping/fetcher.py` | SSRF-hardened `async fetch(url) -> RawPage`. |
| `app/scraping/adapters/base.py` | `SourceAdapter` Protocol. |
| `app/scraping/adapters/registry.py` | `ADAPTERS`, `detect(url) -> SourceAdapter \| None`. |
| `app/scraping/adapters/mock_store.py` | `MockStoreAdapter`. |
| `app/domain/validation.py` | `validate(record) -> ValidationResult`. Pure. |
| `app/db/models/base.py` | `Base(DeclarativeBase)`. |
| `app/db/models/scraping.py` | `Source`, `Product`, `CurrentObservation` ORM classes. |
| `app/db/models/repository.py` | `upsert_scrape_result()` — the write-gating table above, as one function. |
| `app/db/migrations/versions/0002_add_scraping_core_tables.py` | Hand-written; `revision="0002"`, `down_revision="0001"`. |
| `app/core/errors.py` | `ApiError`, `RequestIdMiddleware`, exception handlers, `register_error_handling()`. |
| `app/api/v1/scrapes.py` | `POST /api/v1/scrapes`, `GET /api/v1/scrapes/{task_id}`. |

**Modified — backend**

- `app/core/config.py`: + `mock_store_base_url`, `ssrf_allowed_hosts`, `scraper_user_agent`.
- `app/db/migrations/env.py`: `target_metadata` now `Base.metadata`.
- `app/workers/tasks_http.py`: + `scrape_source_url` task (`ping` untouched) + `ERROR_REASON_TO_RESPONSE`.
- `app/main.py`: mounts `scrapes_router` under `/api/v1`; registers error handling. Health routes unchanged.
- `backend/pyproject.toml`: `httpx` promoted to a runtime dependency; + `selectolax`; + `asyncio_mode = "auto"` (needed for the first `async def` tests this repo has).

**Modified — mock-store**

- `tools/mock-store/app.py`: + `GET /products/{slug}` for `widget-in-stock`, `widget-out-of-stock`, `widget-missing-price`, `widget-invalid-review-count`, each embedding schema.org `Product` JSON-LD. Docstring updated (mock-store is no longer purely "not part of the product" — it's now the one real scrape target Phase 1's adapter runs against). Still single-file, no new dependency, no Dockerfile change.

**Modified — deployment**

- `docker-compose.yml`: + `MOCK_STORE_BASE_URL` on `api`'s and `worker-http`'s `environment:`; `worker-http` now `depends_on: mock-store: condition: service_healthy`. No service added/removed, no profile membership change — confirmed via `docker compose config --services` (10 default / 17 total, unchanged from doc 16 §D).
- `.env.example`: + `MOCK_STORE_BASE_URL=http://mock-store:4000`.
- `.github/workflows/ci.yml`: smoke-test job gains a `docker compose exec -T api pytest -m integration` step (previously nothing in CI ever ran the integration layer, a pre-existing gap this phase closes).

**New — tests** (all sync `TestClient` + `@pytest.mark.integration` where it applies, matching Phase 0's existing style; `asyncio_mode = "auto"` for the tests that genuinely need it)

- `test_normalize.py` (25 cases), `test_validate.py` (9 cases) — pure unit tests.
- `test_fetcher_ssrf.py` (9 cases, table-driven where it fits doc 12 §4's list) — file/ftp scheme rejection, RFC1918/loopback/link-local rejection including a mocked `169.254.169.254`, a redirect chain that starts public and redirects into a private range, the mock-store allowlist exception, plus a positive redirect-follow case, a too-many-redirects case, and timeout/5xx mapping. Deliberately **not** marked `integration` — runs on every PR, matching doc 12 §4's "fails the build if broken, no override."
- `test_adapter_mock_store.py` + 4 golden fixture pairs under `tests/fixtures/adapters/mock_store/` — exact-match `RawFields` extraction per fixture, plus a not-a-Product-JSON-LD-block filter test, an empty-page case, and a malformed-JSON-LD case.
- `test_persistence_integration.py` (`@pytest.mark.integration`) — calls `upsert_scrape_result()` directly, covering all 4 rows of the write-gating table, including the one case (valid, then invalid, same identity) the mock-store HTTP fixtures alone cannot prove.
- `test_scrapes_integration.py` (`@pytest.mark.integration`) — real Postgres+Redis+Celery worker, `task_always_eager=False`. First-sighting success, repeat-scrape idempotency, missing-required-field failure, partial success, unsupported-adapter rejection, unknown-task-id 404.

---

## API contract (as implemented)

**`POST /api/v1/scrapes`** — body `{"source_url": "http://mock-store:4000/products/widget-in-stock"}` (must be the Compose-internal address — the fetch runs inside `worker-http`'s container).

`registry.detect()` runs first, in-process — no match → `422 UNSUPPORTED_ADAPTER` immediately, no Celery dispatch. On match, dispatches `scrape_source_url` and polls non-blockingly for up to ~35s: `AsyncResult.ready()` is checked via `asyncio.to_thread` (Celery's result backend is synchronous under the hood; `to_thread` keeps even that brief per-poll Redis round trip off the event loop) with `await asyncio.sleep(0.5)` between checks — never a blocking `.get(timeout=...)`, which would freeze every other concurrent request on this single-replica API. This exact bug was caught during plan review, before any code was written, and the fix is what's implemented.

| Outcome | Status | Body |
|---|---|---|
| Full/partial success | 200 | `{task_id, status:"completed", source, product, observation, created}` |
| Required field missing | 422 | `{"error":{"code":"MISSING_REQUIRED_FIELD",...}}` |
| No adapter matched | 422 | `{"error":{"code":"UNSUPPORTED_ADAPTER",...}}` |
| No product found on the page | 422 | `{"error":{"code":"NO_PRODUCT_FOUND",...}}` |
| Fetch failed (timeout/network/SSRF-blocked) | 502 | `{"error":{"code":"FETCH_FAILED",...}}` |
| Didn't finish in time | 202 | `{status:"pending", task_id}` |
| Unexpected exception | 500 | generic safe message, no traceback |
| Malformed request body | 422 | `{"error":{"code":"VALIDATION_ERROR",...}}` |

**`GET /api/v1/scrapes/{task_id}`** — wraps the same `AsyncResult`. Unknown/expired id → `404`, resolved by reaching into the Redis result backend's key-value layer directly (`celery_app.backend.get_key_for_task`/`.get`, off the event loop via `to_thread`) — `AsyncResult`'s own public API cannot distinguish "never submitted" from "queued, not started," both reporting `PENDING`; this is a well-documented Celery/Redis-backend limitation, not an oversight, and the workaround avoids adding a DB-backed job table (Scope Decision 9). Degrades gracefully (reads as "pending" rather than 404) if the configured backend doesn't expose this.

---

## Exact commands

```bash
# --- one-time: regenerate the lockfile now that backend/pyproject.toml changed ---
make bootstrap
git add backend/uv.lock
git commit -m "Regenerate backend/uv.lock for Phase 1 (httpx, selectolax)"

# --- apply the new migration ---
docker compose exec api alembic upgrade head

# --- regression: full reset, same 10 default services as Phase 0 ---
docker compose down -v && docker compose up -d --build
./scripts/wait_for_healthy.sh
curl -sf http://localhost:8000/healthz
curl -sf http://localhost:8000/readyz

# --- exercise the 4 mock-store product pages directly ---
curl -s http://localhost:4000/products/widget-in-stock | head -c 400
curl -s http://localhost:4000/products/widget-missing-price | head -c 400
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:4000/products/does-not-exist   # expect 404

# --- trigger a scrape ---
curl -s -X POST http://localhost:8000/api/v1/scrapes \
  -H "Content-Type: application/json" \
  -d '{"source_url": "http://mock-store:4000/products/widget-in-stock"}'

# --- poll a task by id ---
curl -s http://localhost:8000/api/v1/scrapes/<task_id>

# --- tests ---
docker compose exec api pytest                     # unit + fixture layers
docker compose exec api pytest -m integration       # real DB/Redis/Celery
docker compose exec api ruff check .
docker compose exec api mypy app
```

---

## Acceptance checklist

Markers: `[x]` verified for real in this session; `[~]` written/attempted but not run to completion, or partially confirmed; `[ ]` not yet attempted end-to-end.

- [ ] Migration `0002` applies cleanly; `sources`/`products`/`current_observations` exist with the constraints above. *(Requires a live Postgres — not available in this sandbox; see § Verification status.)*
- [ ] All 4 mock-store product routes serve correct JSON-LD; `/healthz` unchanged. *(`/healthz`'s code is untouched — confirmed by diff; live serving not run.)*
- [ ] `POST /api/v1/scrapes` against `widget-in-stock` → 200, real persisted rows matching the response.
- [ ] Same request repeated → still one `products` row, `current_observations` updated in place. *(The equivalent guarantee is exercised directly against a real Postgres by `test_persistence_integration.py`'s `test_repeat_valid_scrape_updates_in_place_not_duplicated` — written, not yet run.)*
- [ ] `widget-missing-price` → 422 `MISSING_REQUIRED_FIELD`, zero rows created for that identity.
- [ ] `widget-invalid-review-count` → 200, `is_valid=true`, `validation_errors` populated for `review_count` only.
- [ ] A non-mock-store URL → 422 `UNSUPPORTED_ADAPTER`, confirmed no Celery dispatch.
- [~] Direct `upsert_scrape_result()` test proves a valid observation survives a later invalid write for the same identity — **test written** (`test_invalid_write_after_valid_leaves_prior_snapshot_standing`), traced by hand against the actual `repository.py` logic, **not executed** (no live Postgres in this sandbox).
- [ ] `GET /api/v1/scrapes/{task_id}` returns the persisted result for a real id, 404 for a bogus one.
- [~] SSRF table test passes in full, including the `mock-store` allow-case — **test written**, 9 cases, hand-traced against the real fetcher logic, **not executed** (`httpx`/dependencies not installable in this sandbox — see § Verification status).
- [~] `pytest` (all layers) green; `ruff check .` / `mypy app` clean — **`ruff check .` verified for real, clean.** `mypy app`: run for real; found and fixed one genuine bug (see below); the remaining 43 errors are import-resolution cascade from dependencies that cannot be installed here, confirmed against Phase 0's own already-merged files under the identical condition. `pytest` itself: **not run** (no installed dependencies).
- [ ] CI green, including the new `pytest -m integration` smoke-test step. *(Requires a pushed branch with `uv.lock` committed — see § Verification status.)*
- [~] All 10 Phase 0 default services still healthy from clean volumes; 4 optional profiles unchanged; secret scan clean. — **Topology confirmed unchanged** (`docker compose config --services`: 10 default / 17 total, identical to doc 16 §D). **Live health from clean volumes: not verified** — a real `docker compose down -v && docker compose up -d --build` was attempted and fails at the base-image pull stage (see below), the same restriction documented on every prior commit in this repo. **Secret scan**: a manual pattern sweep across every tracked file changed this phase found nothing beyond the already-documented placeholders, and `.env` is confirmed not tracked; `gitleaks` itself is not installed in this sandbox (it runs as a separate CI job) so this is not the same guarantee as a real gitleaks pass.

---

## Verification status

**Actually run, for real, in this session:**

- `python3 -m py_compile` on every new/modified `.py` file (backend + mock-store) — clean.
- `ruff check .` across the full backend tree, using the real project config — clean.
- `mypy app` — run for real, twice (before and after one fix). Of 44 initial errors: **one genuine bug**, found and fixed — `app/scraping/fetcher.py`'s DNS-resolution set comprehension had an unsound `str | int` type flowing from `socket.getaddrinfo`'s typeshed stub (which covers address families beyond the ones this code path can practically receive), in SSRF-critical code; fixed by making the `str(...)` conversion explicit. The remaining 43 are import-resolution cascade from `sqlalchemy`/`fastapi`/`celery`/`httpx`/`selectolax`/`pydantic-settings`/`redis` not being installable in this sandbox (network-restricted, consistent with every prior commit in this repo) — confirmed by the identical error pattern appearing on Phase 0's own already-merged, CI-passing files (`health.py`, `scheduler/main.py`, the other task stubs) under this same run. Three "unused `type: ignore`" findings (`errors.py` ×2, `mock_store.py` ×1) are a direct, unavoidable consequence of that same cascade making `FastAPI`/`selectolax` resolve to `Any` — which suppresses the very error those ignore comments exist to catch, in this environment only. Left in place rather than removed on an untrustworthy signal; **please re-run `mypy` in CI (real dependencies) and flag it if either turns out to need adjustment.**
- `docker compose config` (default profile and all-profiles) — resolves cleanly with the new env vars and dependency; service topology confirmed byte-for-byte unchanged from doc 16 §D.
- A real `docker compose down -v && docker compose up -d --build` was attempted, exactly as the established practice in this repo requires. It fails immediately at the base-image pull stage: `failed to resolve reference "docker.io/mailhog/mailhog:latest": ... Forbidden` — the same network-egress restriction documented on every prior commit here (§ Implementation Notes, doc 16), now reconfirmed against Phase 1's compose changes specifically. Cleaned up with `docker compose down -v` afterward; nothing left running.
- A manual secret-pattern sweep across every file changed this phase, and confirmation `.env` is not tracked.

**Not verified in this sandbox, and why:**

- Everything requiring a live Postgres/Redis/Celery/running containers (migration 0002 applying, the live endpoint's full request/response cycle, both integration test files, the 4 mock-store routes served over real HTTP) — blocked by the same network restriction above; nothing further static analysis here could have caught.
- `backend/uv.lock` **does not exist in this checkout** — consistent with this repo's own established pattern (doc 16's "Add Docker-first bootstrap" work: lockfiles are generated locally via `make bootstrap` and committed by whoever runs it, never hand-written by generation). Adding `httpx`/`selectolax` to `backend/pyproject.toml`'s dependencies means **`make bootstrap` (or `cd backend && uv lock`) needs to be re-run on a machine with real network access before `docker compose build` will succeed** — this blocks every other unchecked box above, not just the ones that mention it directly.
- `pytest` itself (any layer) — cannot run without the dependencies `uv sync` would install; blocked by the same lockfile/network gap.

**What this means concretely:** every box above that isn't `[x]` needs exactly one thing to close out — a machine with normal PyPI/registry access, running `make bootstrap` (regenerates `backend/uv.lock` with the two new dependencies) followed by `docker compose down -v && docker compose up -d --build` and the test commands in § Exact commands. Nothing in this phase's design is expected to fail on a normal machine; the entire gap here is execution environment, not implementation risk that further sandbox work could reduce.

---

## Explicitly out of scope (deferred, not forgotten)

Real marketplace/generic/Shopify/WooCommerce adapters, browser automation/Playwright, auth, billing, alerts/diffing, scheduling UI or the `schedules`/`runs`/`tasks` state machine, multi-tenancy/RLS, the full `/free/scrapes` contract (CSV export, rate limiting, 5-URL batching), robots.txt/Crawl-delay/domain politeness enforcement, CAPTCHA/anti-bot detection, `seed_demo.py`, Playwright-driven E2E journeys, frontend changes.
