"""Local HTTP target for Phase 0 smoke-testing and Phase 1's
`MockStoreAdapter` (backend/app/scraping/adapters/mock_store.py).

Phase 0 used this only to sanity-check outbound connectivity from the
worker containers. Phase 1 (doc 17) adds it as the one real scrape target
the system's adapter pipeline runs against end to end -- still not a real
marketplace, but no longer just a placeholder either: `GET
/products/{slug}` pages embed schema.org `Product` JSON-LD, the same
shape a real store's product page would carry, exercising the real
fetch->parse->normalize->validate pipeline without scraping anything on
the public internet.

Four fixed slugs, each exercising a distinct doc 08 §4 outcome:
  - widget-in-stock            full success, in stock
  - widget-out-of-stock        full success, out of stock (still valid --
                                stock_status has a value, it's just not
                                "in_stock")
  - widget-missing-price       `offers.price` deliberately omitted ->
                                required-field failure (422
                                MISSING_REQUIRED_FIELD)
  - widget-invalid-review-count `aggregateRating.reviewCount` deliberately
                                non-numeric -> partial success (is_valid
                                true, validation_errors populated for
                                review_count only)

Deliberately still a single file, no new dependency -- product data is
plain Python dicts, JSON-LD is `json.dumps()`, matching the rest of this
tool's minimal footprint.
"""

import json
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

app = FastAPI(title="Mock Store")

_PRODUCTS: dict[str, dict[str, Any]] = {
    "widget-in-stock": {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": "Acme Widget - Blue",
        "brand": {"@type": "Brand", "name": "ACME"},
        "category": "Home > Widgets",
        "sku": "WIDGET-BLUE-001",
        "productID": "10001",
        "description": (
            "A dependable everyday widget in a calming shade of blue. "
            "Built to last, easy to clean, fits most standard widget mounts."
        ),
        "image": "/images/widget-blue.jpg",
        "offers": {
            "@type": "Offer",
            "price": "29.99",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
        },
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": "4.5",
            "reviewCount": "128",
        },
    },
    "widget-out-of-stock": {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": "Acme Widget - Red",
        "brand": {"@type": "Brand", "name": "ACME"},
        "category": "Home > Widgets",
        "sku": "WIDGET-RED-002",
        "productID": "10002",
        "description": (
            "The same great widget, now in red. Currently unavailable while we restock."
        ),
        "image": "/images/widget-red.jpg",
        "offers": {
            "@type": "Offer",
            "price": "29.99",
            "priceCurrency": "USD",
            "availability": "https://schema.org/OutOfStock",
        },
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": "4.2",
            "reviewCount": "56",
        },
    },
    "widget-missing-price": {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": "Acme Widget - Green (Preview)",
        "brand": {"@type": "Brand", "name": "ACME"},
        "category": "Home > Widgets",
        "sku": "WIDGET-GREEN-003",
        "productID": "10003",
        "description": "Coming soon. Pricing has not been finalized for this listing.",
        "image": "/images/widget-green.jpg",
        "offers": {
            # No "price" key -- deliberate. Exercises doc 08 §4's failure
            # path: task fails with missing_required_field, the previous
            # valid snapshot (if any) for this identity is left standing.
            "@type": "Offer",
            "priceCurrency": "USD",
            "availability": "https://schema.org/PreOrder",
        },
    },
    "widget-invalid-review-count": {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": "Acme Widget - Yellow",
        "brand": {"@type": "Brand", "name": "ACME"},
        "category": "Home > Widgets",
        "sku": "WIDGET-YELLOW-004",
        "productID": "10004",
        "description": "A cheerful yellow widget. Customer-favorite finish.",
        "image": "/images/widget-yellow.jpg",
        "offers": {
            "@type": "Offer",
            "price": "24.50",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
        },
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": "4.8",
            # Deliberately not a number -- exercises doc 08 §4's partial-
            # success path: every required field is still valid, so this
            # is still `is_valid: true`, with validation_errors populated
            # for review_count only.
            "reviewCount": "lots",
        },
    },
}


def _render_product_page(slug: str, product: dict[str, Any]) -> str:
    name = product.get("name", slug)
    json_ld = json.dumps(product)
    return (
        "<!doctype html>"
        "<html><head>"
        f"<title>{name}</title>"
        f'<script type="application/ld+json">{json_ld}</script>'
        "</head>"
        f"<body><h1>{name}</h1>"
        f"<p>{product.get('description', '')}</p>"
        "</body></html>"
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (
        "<!doctype html>"
        "<html><head><title>Mock Store</title></head>"
        "<body><h1>Mock Store</h1>"
        "<p>Placeholder page for local smoke-testing.</p>"
        "<ul>" + "".join(f'<li><a href="/products/{slug}">{slug}</a></li>' for slug in _PRODUCTS)
        + "</ul>"
        "</body></html>"
    )


@app.get("/products/{slug}", response_class=HTMLResponse)
async def product_page(slug: str) -> str:
    product = _PRODUCTS.get(slug)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return _render_product_page(slug, product)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
