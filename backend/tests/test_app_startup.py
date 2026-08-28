"""Unit tests for FastAPI application startup configuration."""

from __future__ import annotations

from fastapi import FastAPI

from app.core.config import Settings
from app.main import create_app


def test_create_app_allows_production_when_authentication_is_configured(
    monkeypatch,
) -> None:
    """Production startup must not require the removed API acknowledgement."""
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(
            environment="production",
            jwt_secret_key="test-secret-that-is-long-enough-for-a-unit-test",
        ),
    )

    app = create_app()

    assert isinstance(app, FastAPI)
