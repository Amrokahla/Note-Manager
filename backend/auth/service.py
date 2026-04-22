from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from backend.auth.models import UserInDB, UserPublic, UsernameTakenError
from backend.auth.passwords import hash_password, verify_password
from backend.db.sqlite import tx


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_public(row: sqlite3.Row) -> UserPublic:
    return UserPublic(
        id=row["id"],
        username=row["username"],
        created_at=row["created_at"],
    )


def _row_to_db(row: sqlite3.Row) -> UserInDB:
    return UserInDB(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        created_at=row["created_at"],
    )


def create_user(username: str, password: str) -> UserPublic:
    """Register a new user. Raises `UsernameTakenError` on UNIQUE collision."""
    hashed = hash_password(password)
    now = _utc_now_iso()
    with tx() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, created_at) "
                "VALUES (?, ?, ?)",
                (username, hashed, now),
            )
        except sqlite3.IntegrityError as e:
            # The only UNIQUE constraint on users is username.
            raise UsernameTakenError(username) from e

        user_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT id, username, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_public(row)


def authenticate(username: str, password: str) -> UserPublic | None:
    """Return the UserPublic if credentials match; else None (generic for 401)."""
    with tx() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, created_at FROM users "
            "WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return None
    user = _row_to_db(row)
    if not verify_password(password, user.password_hash):
        return None
    return UserPublic(
        id=user.id, username=user.username, created_at=user.created_at
    )


def get_by_id(user_id: int) -> UserPublic | None:
    with tx() as conn:
        row = conn.execute(
            "SELECT id, username, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_public(row) if row else None
