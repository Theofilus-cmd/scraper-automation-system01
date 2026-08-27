"""Authentication endpoints: registration, login, and current-user profile."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.api.dependencies import CurrentUser
from app.core.config import get_settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models.identity_repository import (
    EmailAlreadyRegisteredError,
    create_user_with_personal_workspace,
    get_user_by_email,
)
from app.db.session import get_session

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    display_name: str
    is_verified: bool


class RegisterResponse(BaseModel):
    user: UserResponse
    workspace: dict[str, str]
    token: TokenResponse


def _token_response(user_id: str) -> TokenResponse:
    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(subject=user_id),
        expires_in=settings.access_token_expire_minutes * 60,
    )


def _user_response(
    *,
    user_id: str,
    email: str,
    display_name: str,
    is_verified: bool,
) -> UserResponse:
    return UserResponse(
        id=user_id,
        email=email,
        display_name=display_name,
        is_verified=is_verified,
    )


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest) -> RegisterResponse:
    """Create a user, their personal workspace, and an access token."""
    async with get_session() as session:
        try:
            user, workspace = await create_user_with_personal_workspace(
                session,
                email=str(body.email),
                password_hash=hash_password(body.password),
                display_name=body.display_name,
            )
        except EmailAlreadyRegisteredError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            ) from None

    return RegisterResponse(
        user=_user_response(
            user_id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            is_verified=user.is_verified,
        ),
        workspace={"id": str(workspace.id), "name": workspace.name, "slug": workspace.slug},
        token=_token_response(str(user.id)),
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest) -> TokenResponse:
    """Authenticate an active user and return a short-lived access token."""
    async with get_session() as session:
        user = await get_user_by_email(session, str(body.email))

    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return _token_response(str(user.id))


@router.get("/me", response_model=UserResponse)
async def me(current_user: CurrentUser) -> UserResponse:
    """Return the profile of the Bearer-token user."""
    return _user_response(
        user_id=str(current_user.id),
        email=current_user.email,
        display_name=current_user.display_name,
        is_verified=current_user.is_verified,
    )