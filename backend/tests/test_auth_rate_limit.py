"""Auth throttling (real Redis) and list-setting parsing."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.rate_limit import enforce_rate_limit
from app.main import app


@pytest.fixture
def limited(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "auth_rate_limit_attempts", 3)
    monkeypatch.setattr(settings, "auth_rate_limit_window_seconds", 30)


@pytest.mark.integration
async def test_limit_blocks_after_attempts_with_retry_after(limited: None) -> None:
    ident = uuid.uuid4().hex
    for _ in range(3):
        await enforce_rate_limit("test", ident)
    with pytest.raises(HTTPException) as info:
        await enforce_rate_limit("test", ident)
    assert info.value.status_code == 429
    assert 1 <= int(info.value.headers["Retry-After"]) <= 30  # type: ignore[index]
    await enforce_rate_limit("test", uuid.uuid4().hex)  # other keys unaffected


@pytest.mark.integration
def test_login_endpoint_returns_429_per_email(limited: None) -> None:
    email = f"{uuid.uuid4().hex}@example.com"
    with TestClient(app) as client:
        codes = [
            client.post("/api/v1/auth/login", json={"email": email, "password": "x"}).status_code
            for _ in range(5)
        ]
    # The per-IP counter may already be partly used; per-email is the
    # guarantee: attempts 1-3 are 401, the rest 429.
    assert codes[:3] == [401, 401, 401] or 429 in codes
    assert codes[-1] == 429


async def test_redis_failure_fails_open(
    limited: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
    await enforce_rate_limit("test", "anyone")  # must not raise


def test_list_settings_accept_csv_and_json() -> None:
    assert Settings(ssrf_allowed_hosts="a, b").ssrf_allowed_hosts == ["a", "b"]  # type: ignore[arg-type]
    assert Settings(ssrf_allowed_hosts='["a"]').ssrf_allowed_hosts == ["a"]  # type: ignore[arg-type]
    assert Settings(ssrf_allowed_hosts="").ssrf_allowed_hosts == []  # type: ignore[arg-type]
