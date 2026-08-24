"""Minimal static HTTP target for local Phase 0 smoke-testing.

This is NOT a scraping target adapter and not part of the product. It
exists purely so the local Compose stack has something reachable over
plain HTTP to sanity-check outbound connectivity from the worker
containers, without depending on any real third-party website.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Mock Store")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (
        "<!doctype html>"
        "<html><head><title>Mock Store</title></head>"
        "<body><h1>Mock Store</h1>"
        "<p>Placeholder page for local Phase 0 smoke-testing.</p>"
        "</body></html>"
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
