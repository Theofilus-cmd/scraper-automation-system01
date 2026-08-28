"""HTTP integration tests for authentication endpoints."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def _register_payload() -> dict[str, str]:
    suffix = uuid.uuid4().hex
    return {
        "email": f"auth-api-{suffix}@example.com",
        "password": "correct-horse-battery-staple",
        "display_name": "Auth API Test User",
    }

def test_register_creates_user_workspace_and_access_token(client: TestClient) -> None:
    payload = _register_payload()

    response = client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 201
    body = response.json()

    assert set(body) == {"user", "workspace", "token"}
    assert body["user"]["email"] == payload["email"]
    assert body["user"]["display_name"] == payload["display_name"]
    assert body["user"]["is_verified"] is False
    assert body["user"]["id"]

    assert body["workspace"]["id"]
    assert body["workspace"]["name"] == f"{payload['display_name']}'s Workspace"
    assert body["workspace"]["slug"].startswith("auth-api-test-user-")

    assert body["token"]["access_token"]
    assert body["token"]["token_type"] == "bearer"
    assert body["token"]["expires_in"] == 1800


def test_register_normalizes_email_and_rejects_duplicate(client: TestClient) -> None:
    payload = _register_payload()

    first = client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201

    duplicate = client.post(
        "/api/v1/auth/register",
        json={
            **payload,
            "email": f"  {payload['email'].upper()}  ",
        },
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "An account with this email already exists."


def test_login_returns_bearer_token_for_valid_credentials(client: TestClient) -> None:
    payload = _register_payload()

    registered = client.post("/api/v1/auth/register", json=payload)
    assert registered.status_code == 201

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": payload["email"],
            "password": payload["password"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800


def test_login_uses_same_generic_error_for_unknown_email_and_bad_password(
    client: TestClient,
) -> None:
    payload = _register_payload()

    registered = client.post("/api/v1/auth/register", json=payload)
    assert registered.status_code == 201

    unknown_email = client.post(
        "/api/v1/auth/login",
        json={
            "email": f"unknown-{uuid.uuid4().hex}@example.com",
            "password": payload["password"],
        },
    )
    bad_password = client.post(
        "/api/v1/auth/login",
        json={
            "email": payload["email"],
            "password": "wrong-password-value",
        },
    )

    assert unknown_email.status_code == 401
    assert bad_password.status_code == 401
    assert unknown_email.headers["www-authenticate"] == "Bearer"
    assert bad_password.headers["www-authenticate"] == "Bearer"
    assert unknown_email.json()["detail"] == "Incorrect email or password."
    assert bad_password.json()["detail"] == "Incorrect email or password."


def test_me_requires_valid_bearer_token(client: TestClient) -> None:
    unauthenticated = TestClient(client.app)
    try:
        missing_token = unauthenticated.get("/api/v1/auth/me")
    finally:
        unauthenticated.close()

    invalid_token = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer not-a-valid-jwt"},
    )

    assert missing_token.status_code == 401
    assert missing_token.headers["www-authenticate"] == "Bearer"
    assert missing_token.json()["detail"] == "Authentication required."

    assert invalid_token.status_code == 401
    assert invalid_token.headers["www-authenticate"] == "Bearer"
    assert invalid_token.json()["detail"] == "Invalid or expired access token."


def test_me_returns_registered_user_for_register_token(client: TestClient) -> None:
    payload = _register_payload()

    registered = client.post("/api/v1/auth/register", json=payload)
    assert registered.status_code == 201
    registered_body = registered.json()

    response = client.get(
        "/api/v1/auth/me",
        headers={
            "Authorization": f"Bearer {registered_body['token']['access_token']}",
        },
    )

    assert response.status_code == 200
    assert response.json() == registered_body["user"]