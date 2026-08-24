# 08 — Scraper Adapter Interface & Data Schema

## 1. Adapter protocol

Every source (`generic`, `shopify`, `woocommerce`, and later `amazon`/`ebay`/etc.) implements the same interface. Platform-specific knowledge lives *only* inside an adapter's `matches()`/`fetch()`/`parse()` — nothing downstream of `parse()` knows or cares which adapter produced a record.

```python
class RawFields(TypedDict, total=False):
    """Loosely-typed extraction output — one dict per product found on the page."""
    product_name: str | None
    brand: str | None
    category: str | None
    price: str | None            # still a raw string here, e.g. "$39.99" or "39,99 €"
    original_price: str | None
    discount: str | None
    sku: str | None
    product_id: str | None
    variant: str | None
    stock_status_raw: str | None  # source's own wording, e.g. "Only 2 left!"
    rating: str | None
    review_count: str | None
    description: str | None
    image_url: str | None

class SourceAdapter(Protocol):
    slug: ClassVar[str]                 # "generic" | "shopify" | "woocommerce"
    requires_js: ClassVar[bool]         # dispatches to queue=http vs queue=browser (doc 04 §1/§4.4)

    def matches(self, url: str, sniff: SiteSniff) -> float:
        """Confidence 0..1 that this adapter should handle the URL. `sniff` is a
        cheap pre-fetch (HEAD + first bytes) shared across all adapters' matches()
        calls so detection doesn't require a full fetch per candidate adapter."""

    async def fetch(self, url: str, ctx: FetchContext) -> RawPage:
        """MUST go through the shared SSRF-hardened fetcher (doc 03 §3) or the
        Playwright session manager — never a raw httpx/requests/Playwright call."""

    def parse(self, page: RawPage, schema: ExtractionSchema) -> list[RawFields]:
        """One URL can yield multiple records — e.g. a WooCommerce category
        listing page. `schema.enabled_fields` filters what's attempted."""
```

Shared, adapter-agnostic pipeline downstream of `parse()`:

```python
def normalize(raw: RawFields, source_url: str) -> NormalizedRecord: ...   # §3 below
def validate(record: NormalizedRecord) -> ValidationResult: ...            # §4 below
```

## 2. Adapter registry & detection

```python
ADAPTERS: list[SourceAdapter] = [ShopifyAdapter(), WooCommerceAdapter(), GenericAdapter()]

def detect(url: str, sniff: SiteSniff) -> SourceAdapter:
    scored = [(a, a.matches(url, sniff)) for a in ADAPTERS]
    best, confidence = max(scored, key=lambda pair: pair[1])
    return best if confidence > 0.3 else GenericAdapter()   # generic is always the floor, never "no adapter"
```

Detection signals, checked cheaply (from the `SiteSniff` — a HEAD request plus a capped read of the first ~50KB, not a full adapter-specific fetch) before committing to a full `fetch()`:

| Adapter | Signals (any one is strong; combination raises confidence) |
|---|---|
| `shopify` | `/products.json` or `/products/<handle>.json` resolves with a `products` key; `Shopify.shop` in inline JS; `<meta name="shopify-digital-wallet">`; `cdn.shopify.com` asset URLs |
| `woocommerce` | `/wp-json/wc/store/v1/products` resolves; `<meta name="generator" content="WooCommerce...">`; `wp-content`/`wp-json` paths present alongside `add-to-cart` form markup |
| `generic` | Fallback for everything else — but tries structured data first (below), so "generic" doesn't mean "worse," just "not platform-API-backed" |

`targets.adapter_type` (doc 05 §4) is set from this at target-save time and re-verified periodically (a target can migrate platforms) rather than re-detected on every single run — detection is comparatively expensive and platform migrations are rare.

**Generic adapter's own internal fallback chain** (tried in order, first that yields a usable `product_name` + `price` wins): (1) schema.org `Product` JSON-LD (`<script type="application/ld+json">`) — the most reliable signal, present on a large share of e-commerce sites regardless of platform; (2) OpenGraph/Twitter meta tags (`og:title`, `product:price:amount`, etc.) — thinner but still structured; (3) heuristic CSS selectors (common class/id patterns like `.price`, `[itemprop=price]`) as a last resort, lowest confidence, and the one place `extraction_schemas.custom_selectors` (doc 05 §4) lets a user hand-supply selectors for a specific stubborn target.

## 3. Canonical data schema (17 fields + system metadata)

| Field | Type | Required | Normalization |
|---|---|---|---|
| `product_name` | string | **yes** | trimmed, collapsed whitespace, HTML entities decoded |
| `brand` | string | no | trimmed; title-cased only if source is ALL CAPS |
| `category` | string | no | trimmed; breadcrumb-style sources joined with `>` |
| `price` | decimal(12,2) | **yes** | strip currency symbols/thousands separators, locale-aware decimal parsing (`,` vs `.`) |
| `currency` | ISO 4217 (3-letter) | **yes** | explicit source value if present, else inferred from symbol (`$`→ locale-dependent, resolved via the page's detected locale/TLD, defaulting `USD` only as last resort) |
| `original_price` | decimal(12,2) | no | same rule as `price` |
| `discount` | decimal(6,2) (%) | no | source-reported if given; else derived as `(original_price - price) / original_price` when both prices present |
| `sku` | string | no | trimmed |
| `product_id` | string | no | trimmed; platform-native ID when the adapter has one (Shopify numeric ID, WooCommerce post ID) |
| `variant` | string | no | trimmed (e.g. "Red / Large") |
| `stock_status` | enum: `in_stock`,`out_of_stock`,`preorder`,`unknown` | **yes** | mapped from wide source vocabulary ("In stock", "Add to cart" button present → `in_stock`; "Sold out", "Out of stock" → `out_of_stock`; "Pre-order", "Coming soon" → `preorder`; unparseable → `unknown`, never guessed as in-stock |
| `rating` | decimal(3,2), 0–5 | no | parsed from "4.5 out of 5" / `ratingValue` schema.org, out-of-range values discarded not clamped |
| `review_count` | integer ≥0 | no | digits-only parse, discarded if non-numeric |
| `description` | string | no | trimmed, truncated at a sane cap (e.g. 5,000 chars) to bound storage |
| `image_url` | absolute URL | no | relative URLs resolved against the page URL; must pass the same scheme allowlist as target URLs (doc 03 §3) |
| `product_url` | absolute URL | **yes** | canonicalized (strip tracking params) |
| `scraped_at` | timestamptz | **yes**, system-set | never taken from source; set by the worker at fetch time |

## 4. Validation & task-outcome mapping

Required fields: `product_name, price, currency, stock_status, product_url` (plus system-set `scraped_at`).

| Outcome | Condition | Task status |
|---|---|---|
| Full success | All required fields present and valid; all requested optional fields that the adapter attempted also parsed cleanly | `succeeded`, `records.is_valid = true` |
| Partial success | All required fields valid; ≥1 optional field failed to parse/coerce | `succeeded`, `records.is_valid = true`, `validation_errors` populated with the optional-field failures — **still a success**, because the core commercial facts (name, price, stock) are trustworthy |
| Failure | Any required field missing or fails validation | `failed`, reason `missing_required_field`, `records` row is **not** written/updated — the previous valid snapshot is left standing rather than overwritten with garbage |

This is the concrete mechanism behind "do not claim a scrape succeeded if validation fails" (doc 00/02/03) — it's enforced at the point of writing to `records`, not left to callers to check.

## 5. Why Shopify/WooCommerce get first-class adapters instead of generic-only

Both platforms expose stable, documented, public JSON endpoints for product data (`/products.json` for Shopify, the WooCommerce Store API) that are far more reliable than DOM scraping — fewer required fields fall back to heuristic CSS selectors, and markup redesigns (which break generic/heuristic parsing) don't break the adapter. WooCommerce sites that don't expose the Store API (older installs, or it's disabled) fall back to structured DOM parsing within the same adapter (`add-to-cart` form fields, `woocommerce-Price-amount` markup conventions) before falling further back to the generic adapter's chain.
