from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import service as auth_service
from backend.auth import tokens
from backend.auth.dependencies import current_user
from backend.auth.models import (
    LoginIn,
    RegisterIn,
    TokenOut,
    UserPublic,
    UsernameTakenError,
)
from backend.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201, response_model=UserPublic)
def register(body: RegisterIn) -> UserPublic:
    try:
        return auth_service.create_user(body.username, body.password)
    except UsernameTakenError:
        raise HTTPException(status_code=409, detail="Username already taken")


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn) -> TokenOut:
    user = auth_service.authenticate(body.username, body.password)
    if user is None:
        # Generic message on purpose — avoids user enumeration.
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = tokens.sign(user.id, user.username)
    return TokenOut(
        access_token=token,
        expires_in=settings.auth_token_ttl_minutes * 60,
        user=user,
    )


@router.get("/me", response_model=UserPublic)
def me(user: UserPublic = Depends(current_user)) -> UserPublic:
    return user
