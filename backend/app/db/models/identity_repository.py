"""Repository functions for users, workspaces, and memberships."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.identity import User, Workspace, WorkspaceMember

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


class EmailAlreadyRegisteredError(Exception):
    """Raised when a registration email is already in use."""


def normalize_email(email: str) -> str:
    """Normalize email before storage and uniqueness checks."""
    return email.strip().lower()


def make_workspace_slug(display_name: str, user_id: uuid.UUID) -> str:
    """Return a deterministic, globally unique initial workspace slug."""
    base = _SLUG_INVALID_CHARS.sub("-", display_name.strip().lower()).strip("-")
    base = base[:40] or "workspace"
    return f"{base}-{str(user_id)[:8]}"


async def create_user_with_personal_workspace(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    display_name: str,
) -> tuple[User, Workspace]:
    """Create a user plus their owner workspace atomically."""
    normalized_email = normalize_email(email)
    normalized_display_name = display_name.strip()
    if not normalized_display_name:
        raise ValueError("display_name must not be empty")

    user = User(
        email=normalized_email,
        password_hash=password_hash,
        display_name=normalized_display_name,
        is_active=True,
        is_verified=False,
    )

    try:
        async with session.begin():
            session.add(user)
            await session.flush()

            workspace = Workspace(
                name=f"{user.display_name}'s Workspace",
                slug=make_workspace_slug(user.display_name, user.id),
                owner_user_id=user.id,
            )
            session.add(workspace)
            await session.flush()

            session.add(
                WorkspaceMember(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role="owner",
                )
            )
    except IntegrityError as exc:
        raise EmailAlreadyRegisteredError(normalized_email) from exc

    await session.refresh(user)
    await session.refresh(workspace)
    return user, workspace
async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Look up a user by UUID."""
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()

async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Look up a user by their normalized email address."""
    normalized_email = normalize_email(email)
    result = await session.execute(
        select(User).where(User.email == normalized_email)
    )
    return result.scalar_one_or_none()