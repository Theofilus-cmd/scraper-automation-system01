# PRODUCT_BLUEPRINT — Competitive Price & Market Intelligence SaaS

Commercial product blueprint. Engineering handoff lives in `PROJECT_CONTEXT.md`;
low-level design notes in `docs/design/`.

> **Provenance note.** This file was seeded from the summary given when the
> blueprint was pulled (markets, phase status, Phase 4 scope). The detailed
> Phase 5–8 specs were not part of that summary and are marked
> **scope TBD** below — fill them in from the master blueprint before starting
> each phase. Do not treat the TBD bullets as decided requirements.

## 1. Target market

US / Global e-commerce only. No Indonesian/local marketplaces.

| Marketplace | Adapter status |
| --- | --- |
| Shopify (public stores) | **IMPLEMENTED** (`products.json` + JSON-LD fallback) |
| Amazon (US) | TODO |
| eBay (US) | TODO |
| Etsy | TODO |
| TikTok Shop US | TODO |
| Alibaba.com | TODO |

Local dev target: `mock_store` adapter (deterministic fixtures).

## 2. Status summary

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Walking skeleton (compose, health, CI basics) | IMPLEMENTED |
| 1 | Scraping core (adapters, normalize, validate, persistence) | IMPLEMENTED |
| 2 | Source lifecycle, scheduling, runs, observation history | IMPLEMENTED |
| 3 | Reliability & politeness (leases, rate limits, retries, dead letter), auth/workspaces | IMPLEMENTED |
| — | Shopify adapter | IMPLEMENTED |
| — | Basic change alerts (price / stock) + email via MailHog | IMPLEMENTED |
| — | Multi-page UI (charts, alerts, dead-letter views) | IMPLEMENTED |
| 4 | **Product Intelligence Core** (own catalog, matching, price gap/position, MAP) | **IMPLEMENTED (backend API; UI TODO)** |
| 5 | Scope TBD (see master blueprint) | TODO |
| 6 | Scope TBD (see master blueprint) | TODO |
| 7 | Scope TBD (see master blueprint) | TODO |
| 8 | Scope TBD (see master blueprint) | TODO |

## 3. Phase 4 — Product Intelligence Core

Goal: let a user describe **their own catalog** and compare it with scraped
competitor products.

### 3.1 Data model (migration `0008`)

- `internal_products` — workspace-owned catalog: `name`, `sku`, `barcode`
  (GTIN, 8–14 digits), `brand`, `category`, `own_price`, `map_price`
  (nullable), `currency` (ISO-4217 alpha-3), `status` (`active`|`archived`).
  SKU unique per workspace, case-insensitive.
- `product_matches` — links an internal product to a scraped `products` row.
  `match_type`: `sku` (auto), `url` (auto, by normalized URL), `manual`.
  Unique per (internal product, competitor product). Both sides must belong
  to the same workspace.

### 3.2 API (`/api/v1`, all workspace-scoped)

- `/internal-products` — `POST` (201), `GET` (cursor-paginated; filters
  `status`, `sku`, `q`), `GET|PATCH /{id}`, `DELETE /{id}` (archives).
- `/product-matches` — `POST` (by `product_id` → manual, or `product_url` →
  url), `GET` (filters), `GET|DELETE /{id}`, `POST /auto-match` (SKU match).
- `/price-intelligence` — `GET` (per internal product summary; filter
  `map_violation=true`), `GET /{internal_product_id}` (per-competitor detail).

### 3.3 Business rules

- `price_gap = own_price − competitor_price`;
  `price_gap_pct = price_gap / competitor_price × 100` (2 dp).
  Positive = we are more expensive.
- Only competitors whose **currency equals** the internal product currency
  and whose observation is valid are compared (no FX conversion yet);
  others are listed under `excluded` with a reason.
- `price_position` vs. compared competitors: `lowest` (own ≤ min, unless all
  equal), `highest` (own ≥ max), otherwise `average`; `null` with no data.
  A tie where own = min = max reports `average`.
- **MAP violation**: when `map_price` is set, a competitor advertising below
  `map_price` is a violation; `own_price < map_price` is flagged as
  `own_price_below_map`.

### 3.4 Known limits / follow-ups

- No currency conversion, no frontend pages for Phase 4 yet.
- URL auto-match happens on explicit `product_url` match creation; there is
  no internal-product URL field.

## 4. Phases 5–8 — TODO (scope TBD)

Fill each section from the master blueprint before implementation.

- **Phase 5** — TBD
- **Phase 6** — TBD
- **Phase 7** — TBD
- **Phase 8** — TBD
