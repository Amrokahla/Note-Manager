from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from backend.config import settings

_ALGORITHM = "HS256"


def _require_secret() -> str:
    secret = settings.auth_secret
    if not secret:
        # Mirror the lifespan guard: never silently fall back to a default.
        raise RuntimeError(
            "AUTH_SECRET is not set — cannot sign or verify tokens."
        )
    return secret


def sign(user_id: int, username: str) -> str:
    secret = _require_secret()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int(
            (now + timedelta(minutes=settings.auth_token_ttl_minutes)).timestamp()
        ),
    }
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def verify(token: str) -> dict[str, Any]:
    """Decode + validate signature/expiry. Raises jwt.InvalidTokenError on failure."""
    secret = _require_secret()
    return jwt.decode(token, secret, algorithms=[_ALGORITHM])
