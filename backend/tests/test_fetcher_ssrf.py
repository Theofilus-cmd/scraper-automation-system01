"""SSRF hardening tests for app/scraping/fetcher.py (doc 03 §3, doc 12
§4). Security-critical per doc 12 §4: "fails the build if broken, no
override" -- deliberately NOT marked @pytest.mark.integration, so this
runs in the fast `pytest -m "not integration"` CI job on every PR, not
only the Docker-based smoke-test job.

DNS resolution is always mocked (`fetcher._resolve_host` monkeypatched) --
no real network access, no real DNS, ever, in this file. HTTP responses
are mocked via httpx.MockTransport (the fetcher's explicit testability
hook, never used in production code).
"""

import httpx
import pytest

from app.scraping import fetcher
from app.scraping.fetcher import FetchError


def _patch_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    def fake_resolve_host(hostname: str) -> list[str]:
        if hostname not in mapping:
            raise FetchError("dns_or_ssrf_blocked", f"could not resolve host: {hostname}")
        return mapping[hostname]

    monkeypatch.setattr(fetcher, "_resolve_host", fake_resolve_host)


@pytest.mark.parametrize(
    ("url", "dns_map"),
    [
        pytest.param("file:///etc/passwd", {}, id="file-scheme"),
        pytest.param(
            "ftp://public.example.com/x",
            {"public.example.com": ["93.184.216.34"]},
            id="ftp-scheme",
        ),
        pytest.param(
            "http://internal.example.com/x",
            {"internal.example.com": ["10.0.0.5"]},
            id="rfc1918-private",
        ),
        pytest.param(
            "http://loopback.example.com/x",
            {"loopback.example.com": ["127.0.0.1"]},
            id="loopback",
        ),
        pytest.param(
            "http://linklocal.example.com/x",
            # 169.254.169.254 -- the canonical cloud-provider metadata
            # endpoint SSRF target -- doc 12 §4 names this exact address.
            {"linklocal.example.com": ["169.254.169.254"]},
            id="link-local-cloud-metadata",
        ),
    ],
)
async def test_fetch_rejects_disallowed_url(
    monkeypatch: pytest.MonkeyPatch, url: str, dns_map: dict[str, list[str]]
) -> None:
    _patch_dns(monkeypatch, dns_map)

    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch(url)

    assert exc_info.value.reason == "dns_or_ssrf_blocked"


async def test_fetch_rejects_redirect_into_private_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns(
        monkeypatch,
        {
            "public.example.com": ["93.184.216.34"],
            "internal.example.com": ["10.0.0.9"],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example.com":
            return httpx.Response(302, headers={"location": "http://internal.example.com/secret"})
        # The redirect must never actually be followed to the private host.
        raise AssertionError(f"unexpected request reached {request.url}")

    transport = httpx.MockTransport(handler)

    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch("http://public.example.com/start", transport=transport)

    assert exc_info.value.reason == "dns_or_ssrf_blocked"


async def test_fetch_allows_mock_store_despite_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # mock-store resolves to a real Docker-network-private address, which
    # the private-IP rule would otherwise block -- settings.ssrf_allowed_hosts
    # (default: ["mock-store"]) is the one, narrow, explicit exception.
    _patch_dns(monkeypatch, {"mock-store": ["172.18.0.5"]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>ok</html>")

    transport = httpx.MockTransport(handler)

    page = await fetcher.fetch(
        "http://mock-store:4000/products/widget-in-stock", transport=transport
    )

    assert page.status_code == 200
    assert "ok" in page.html


async def test_fetch_follows_redirect_when_target_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns(
        monkeypatch, {"a.example.com": ["93.184.216.1"], "b.example.com": ["93.184.216.2"]}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example.com":
            return httpx.Response(302, headers={"location": "http://b.example.com/final"})
        return httpx.Response(200, text="final page")

    transport = httpx.MockTransport(handler)
    page = await fetcher.fetch("http://a.example.com/start", transport=transport)

    assert page.status_code == 200
    assert page.final_url == "http://b.example.com/final"
    assert page.html == "final page"


async def test_fetch_raises_after_too_many_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dns(monkeypatch, {"loop.example.com": ["93.184.216.1"]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://loop.example.com/next"})

    transport = httpx.MockTransport(handler)

    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch("http://loop.example.com/start", transport=transport)

    assert exc_info.value.reason == "network_error"


async def test_fetch_maps_timeout_to_timeout_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dns(monkeypatch, {"slow.example.com": ["93.184.216.1"]})

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    transport = httpx.MockTransport(handler)

    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch("http://slow.example.com/x", transport=transport)

    assert exc_info.value.reason == "timeout"


async def test_fetch_maps_5xx_to_network_error_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dns(monkeypatch, {"down.example.com": ["93.184.216.1"]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)

    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch("http://down.example.com/x", transport=transport)

    assert exc_info.value.reason == "network_error"
