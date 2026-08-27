"""Password hashing and JWT token helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

password_hasher = PasswordHash.recommended()


class InvalidTokenError(Exception):
    """Raised when an access token cannot be authenticated."""


def hash_password(password: str) -> str:
    """Return an Argon2 password hash; never store plaintext passwords."""
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return whether a plaintext password matches an Argon2 hash."""
    return password_hasher.verify(password, password_hash)


def create_access_token(*, subject: str) -> str:
    """Create a signed, short-lived JWT access token for a user UUID."""
    settings = get_settings()
    if not settings.jwt_secret_key:
        raise RuntimeError(
            "JWT_SECRET_KEY is not configured. Set a cryptographically random secret in the environment."
        )

    expires_at = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "exp": expires_at,
        "iat": datetime.now(UTC),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str:
    """Validate an access token and return its subject/user UUID."""
    settings = get_settings()
    if not settings.jwt_secret_key:
        raise InvalidTokenError("JWT secret is not configured.")

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp", "iat", "type"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("Invalid or expired access token.") from exc

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject or payload.get("type") != "access":
        raise InvalidTokenError("Invalid access token payload.")
    return subject
