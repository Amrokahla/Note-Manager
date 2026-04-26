from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class UserPublic(BaseModel):
    """Shape returned to API clients — never carries the password hash."""

    id: int
    username: str
    created_at: datetime


class RegisterIn(BaseModel):
    # Username regex keeps it URL-safe and unambiguous for v1.
    username: str = Field(
        ..., min_length=3, max_length=40, pattern=r"^[a-zA-Z0-9_.-]+$"
    )
    password: str = Field(..., min_length=8, max_length=200)


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserPublic


class UsernameTakenError(Exception):
    """Raised by auth_service.create_user when the username is in use."""
