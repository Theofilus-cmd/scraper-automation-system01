"""Reusable FastAPI dependencies for authentication and authorization."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import InvalidTokenError, decode_access_token
from app.db.models.identity_repository import get_user_by_id
from app.db.session import get_session
from sqlalchemy import select

from app.db.models.identity import User, Workspace, WorkspaceMember

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    """Return the active user represented by a Bearer access token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = uuid.UUID(decode_access_token(credentials.credentials))
    except (InvalidTokenError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive or no longer exists.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

async def get_current_workspace(current_user: CurrentUser) -> Workspace:
    """Return the current user's first workspace membership.

    This is a temporary single-workspace selection policy. Add an explicit
    workspace selector before enabling multi-workspace switching in the UI.
    """
    async with get_session() as session:
        workspace = (
            await session.execute(
                select(Workspace)
                .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
                .where(WorkspaceMember.user_id == current_user.id)
                .order_by(WorkspaceMember.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not belong to a workspace.",
        )

    return workspace


CurrentWorkspace = Annotated[Workspace, Depends(get_current_workspace)]