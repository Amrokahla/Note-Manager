from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.db.sqlite import tx

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Append-only: never edit or reorder existing entries — schema_version tracks them by index.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS notes (
          id                    INTEGER PRIMARY KEY AUTOINCREMENT,
          title                 TEXT    NOT NULL,
          description           TEXT    NOT NULL,
          tag                   TEXT,
          embedding             BLOB,
          embedding_updated_at  TEXT,
          created_at            TEXT    NOT NULL,
          updated_at            TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_notes_tag
          ON notes(tag) WHERE tag IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_notes_updated_at
          ON notes(updated_at DESC);
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS users (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          username      TEXT    NOT NULL UNIQUE,
          password_hash TEXT    NOT NULL,
          created_at    TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

        -- Existing rows land on user_id=0 (no FK row exists yet); documented
        -- as an operator cleanup step in README before multi-user use.
        ALTER TABLE notes ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
        DROP INDEX IF EXISTS idx_notes_updated_at;
        CREATE INDEX IF NOT EXISTS idx_notes_user_id_updated_at
          ON notes(user_id, updated_at DESC);
        """,
    ),
]


def run_migrations() -> None:
    """Apply any pending migrations in order. Idempotent."""
    with tx() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
              version    INTEGER PRIMARY KEY,
              applied_at TEXT    NOT NULL
            )
            """
        )
        applied = {
            row[0] for row in conn.execute("SELECT version FROM schema_version")
        }
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (version, _utc_now_iso()),
            )
            logger.info("Applied migration %d", version)
