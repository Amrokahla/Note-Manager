from __future__ import annotations

import bcrypt

from backend.config import settings


def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=settings.auth_bcrypt_cost)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash or non-utf8 input → never authenticate.
        return False
