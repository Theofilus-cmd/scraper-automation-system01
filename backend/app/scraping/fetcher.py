"""SSRF-hardened fetch (doc 03 §3, doc 12 §4) -- the ONLY way any adapter's
fetch() may reach the network (doc 08 §1: "MUST go through the shared
SSRF-hardened fetcher... never a raw httpx/requests/Playwright call").

Security-critical, treated as such (doc 12 §4: "fails the build if broken,
no override"):
  - scheme allowlist (http/https only)
  - DNS resolution checked at fetch time against the resolved IP, not just
    the literal hostname (a public hostname can resolve to a private IP --
    DNS rebinding)
  - every redirect hop is re-validated before being followed (httpx's own
    automatic redirect handling is never used, since it would fetch the
    unsafe hop before any check runs), capped at 5 hops
  - connect/read/total timeouts on every request
  - an honest, identifying User-Agent sent on every request (doc 03 §4)

One narrow, explicit exception: `settings.ssrf_allowed_hosts` lets the
local `mock-store` service (which resolves to a private Docker-network
address) through the private-IP check -- see doc 17's "SSRF allowlist"
section for why this doesn't weaken protection against real internal
services (Postgres/Redis/etc. are never in this list).
"""

from __future__ import annotations

import ipaddress
import socket
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.scraping.types import RawPage

logger = get_logger(__name__)

ALLOWED_SCHEMES = {"http", "https"}
MAX_REDIRECTS = 5
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0
TOTAL_TIMEOUT_SECONDS = 30.0


class FetchError(Exception):
    """Raised for any fetch failure doc 05 §5's `tasks.error_reason` has a
    name for. `reason` is one of that column's values, e.g.
    "dns_or_ssrf_blocked", "timeout", "network_error".

    `retry_after` (doc 18 §5.3, Phase 2): populated only for a 429/503
    response that carried a `Retry-After` header in the (far more common)
    delay-in-seconds form -- the HTTP-date form is deliberately not parsed
    here (flagged, not silently mishandled: a date-form header just leaves
    this `None`, and the caller falls back to Celery's own backoff+jitter,
    which is always a safe, correct default). `None` for every other
    reason/response shape, including a 429/503 with no such header.
    """

    def __init__(self, reason: str, message: str, *, retry_after: float | None = None) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(message)


def _parse_retry_after_seconds(header_value: str | None) -> float | None:
    if not header_value:
        return None
    try:
        seconds = float(header_value.strip())
    except ValueError:
        # HTTP-date form (e.g. "Wed, 21 Oct 2026 07:28:00 GMT") -- not
        # parsed here, see FetchError's docstring.
        return None
    return seconds if seconds >= 0 else None


def _is_blocked_address(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_private or addr.is_loopback or addr.is_link_local


def _resolve_host(hostname: str) -> list[str]:
    """Isolated so tests can monkeypatch DNS resolution (doc 12 §4's
    "mocked 169.254.169.254 resolution" case) without real network access.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise FetchError("dns_or_ssrf_blocked", f"could not resolve host: {hostname}") from exc
    # info[4] (sockaddr) is a tuple whose typeshed stub varies by address
    # family (AF_INET/AF_INET6 give (host, port[, ...]); AF_PACKET gives
    # (ifindex, ...): a mypy-visible str | int union on info[4][0], even
    # though every family this call can practically return here has a
    # string address first. str(...) makes that explicit rather than
    # leaving a real, if currently theoretical, type gap in SSRF-critical
    # code.
    return sorted({str(info[4][0]) for info in infos})


def _check_url_allowed(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetchError("dns_or_ssrf_blocked", f"scheme not allowed: {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise FetchError("dns_or_ssrf_blocked", "url has no hostname")

    settings = get_settings()
    if hostname.lower() in {h.lower() for h in settings.ssrf_allowed_hosts}:
        return  # explicit, narrow local-dev exception -- see module docstring

    for ip in _resolve_host(hostname):
        if _is_blocked_address(ip):
            raise FetchError(
                "dns_or_ssrf_blocked",
                f"host {hostname!r} resolves to a private/reserved address ({ip})",
            )


def validate_source_url(url: str) -> None:
    """Create-time SSRF check (doc 18 §7.1, Phase 2) -- a synchronous
    fast-fail for `POST /sources`, reusing this exact same validation logic
    rather than a separate, parallel implementation that could drift out of
    sync with it. Raises `FetchError` on rejection, same as a real fetch
    would.

    Explicitly a UX/data-quality improvement layered on top of the real
    security boundary, NOT a replacement for it: DNS can change between
    create-time and fetch-time (DNS rebinding), which is exactly why
    `_check_url_allowed` above still runs, unmodified, on every actual
    fetch and every redirect hop -- that check, not this one, is what
    actually protects the network. This function only ever runs the
    single-URL check (no redirect loop -- there's nothing to redirect yet,
    the source hasn't been fetched).
    """
    _check_url_allowed(url)


async def fetch(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> RawPage:
    """Fetch `url`, following redirects manually (one hop at a time, each
    re-validated) up to `MAX_REDIRECTS`. `transport` is a testability hook
    (e.g. `httpx.MockTransport`) -- production code never passes it.
    """
    settings = get_settings()
    headers = {"User-Agent": settings.scraper_user_agent}
    current_url = url

    async with httpx.AsyncClient(
        follow_redirects=False,
        transport=transport,
        timeout=httpx.Timeout(
            TOTAL_TIMEOUT_SECONDS,
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
        ),
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _check_url_allowed(current_url)

            try:
                response = await client.get(current_url, headers=headers)
            except httpx.TimeoutException as exc:
                raise FetchError("timeout", str(exc)) from exc
            except httpx.HTTPError as exc:
                raise FetchError("network_error", str(exc)) from exc

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise FetchError(
                        "network_error", "redirect response missing Location header"
                    )
                current_url = urljoin(current_url, location)
                continue

            if response.status_code in (429, 503):
                raise FetchError(
                    "network_error",
                    f"server returned {response.status_code}",
                    retry_after=_parse_retry_after_seconds(response.headers.get("retry-after")),
                )
            if response.status_code >= 500:
                raise FetchError("network_error", f"server returned {response.status_code}")

            return RawPage(
                html=response.text,
                final_url=str(response.url),
                status_code=response.status_code,
                fetched_at=datetime.now(UTC),
            )

    raise FetchError("network_error", f"too many redirects (> {MAX_REDIRECTS})")
