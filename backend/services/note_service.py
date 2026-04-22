from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from backend.config import settings
from backend.db.sqlite import tx
from backend.services import embeddings
from backend.services.models import Note, NoteSummary, TagCount

logger = logging.getLogger(__name__)


def normalize_tag(t: str | None) -> str | None:
    if t is None:
        return None
    cleaned = t.strip().lstrip("#").lower()
    return cleaned or None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        tag=row["tag"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_summary(row: sqlite3.Row, similarity: float | None = None) -> NoteSummary:
    return NoteSummary(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        tag=row["tag"],
        updated_at=row["updated_at"],
        similarity=similarity,
    )


def _compose_embed_input(title: str, description: str) -> str:
    return f"{title}\n\n{description}"


def _try_embed(title: str, description: str) -> tuple[bytes | None, str | None]:
    """Return (blob, timestamp) for the embedding, or (None, None) if Ollama is down."""
    try:
        vec = embeddings.embed(_compose_embed_input(title, description))
        return embeddings.to_blob(vec), _utc_now_iso()
    except Exception as e:
        logger.warning("Embedding failed for note: %s", e)
        return None, None


def create_note(title: str, description: str, tag: str | None = None) -> Note:
    now = _utc_now_iso()
    norm_tag = normalize_tag(tag)
    embedding_blob, embedding_ts = _try_embed(title, description)

    with tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO notes(title, description, tag, embedding, embedding_updated_at,
                              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (title, description, norm_tag, embedding_blob, embedding_ts, now, now),
        )
        note_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT id, title, description, tag, created_at, updated_at "
            "FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
    return _row_to_note(row)


def get_note(note_id: int) -> Note | None:
    with tx() as conn:
        row = conn.execute(
            "SELECT id, title, description, tag, created_at, updated_at "
            "FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
    return _row_to_note(row) if row else None


def update_note(
    note_id: int,
    title: str | None = None,
    description: str | None = None,
    tag: str | None = None,
    *,
    clear_tag: bool = False,
) -> Note | None:
    """Patch an existing note; `clear_tag=True` sets tag to NULL explicitly."""
    with tx() as conn:
        existing = conn.execute(
            "SELECT title, description FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        if existing is None:
            return None

        set_clauses: list[str] = []
        args: list = []
        if title is not None:
            set_clauses.append("title = ?")
            args.append(title)
        if description is not None:
            set_clauses.append("description = ?")
            args.append(description)
        if clear_tag:
            set_clauses.append("tag = NULL")
        elif tag is not None:
            set_clauses.append("tag = ?")
            args.append(normalize_tag(tag))

        text_changed = title is not None or description is not None
        if text_changed:
            new_title = title if title is not None else existing["title"]
            new_desc = description if description is not None else existing["description"]
            blob, ts = _try_embed(new_title, new_desc)
            set_clauses.append("embedding = ?")
            args.append(blob)
            set_clauses.append("embedding_updated_at = ?")
            args.append(ts)

        if not set_clauses:
            row = conn.execute(
                "SELECT id, title, description, tag, created_at, updated_at "
                "FROM notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            return _row_to_note(row)

        set_clauses.append("updated_at = ?")
        args.append(_utc_now_iso())
        args.append(note_id)
        conn.execute(
            f"UPDATE notes SET {', '.join(set_clauses)} WHERE id = ?",
            args,
        )
        row = conn.execute(
            "SELECT id, title, description, tag, created_at, updated_at "
            "FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
    return _row_to_note(row)


def delete_note(note_id: int) -> bool:
    with tx() as conn:
        cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return cur.rowcount > 0


def list_notes(tag: str | None = None, limit: int = 10) -> list[NoteSummary]:
    norm_tag = normalize_tag(tag) if tag else None
    with tx() as conn:
        if norm_tag is not None:
            rows = conn.execute(
                """
                SELECT id, title, description, tag, updated_at
                FROM notes WHERE tag = ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (norm_tag, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, title, description, tag, updated_at
                FROM notes ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_summary(r) for r in rows]


def list_tags(limit: int = 4) -> list[TagCount]:
    """Return the top-N tags by usage count."""
    with tx() as conn:
        rows = conn.execute(
            """
            SELECT tag, COUNT(*) AS c
            FROM notes
            WHERE tag IS NOT NULL
            GROUP BY tag
            ORDER BY c DESC, tag ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [TagCount(tag=r["tag"], count=r["c"]) for r in rows]


def search_semantic(
    query: str,
    limit: int = 5,
    threshold: float | None = None,
    fallback_limit: int | None = None,
) -> tuple[list[NoteSummary], bool]:
    """Rank notes by cosine similarity; returns (results, above_threshold)."""
    if threshold is None:
        threshold = settings.search_threshold
    if fallback_limit is None:
        fallback_limit = settings.search_fallback_limit

    q_vec = embeddings.embed(query)

    with tx() as conn:
        rows = conn.execute(
            """
            SELECT id, title, description, tag, embedding, updated_at
            FROM notes WHERE embedding IS NOT NULL
            """
        ).fetchall()

    if not rows:
        return [], False

    scored: list[tuple[float, sqlite3.Row]] = []
    for r in rows:
        v = embeddings.from_blob(r["embedding"])
        sim = embeddings.cosine(q_vec, v)
        scored.append((sim, r))
    scored.sort(key=lambda x: -x[0])

    above = [t for t in scored if t[0] >= threshold]
    if above:
        return [_row_to_summary(r, similarity=s) for s, r in above[:limit]], True

    return [_row_to_summary(r, similarity=s) for s, r in scored[:fallback_limit]], False


def backfill_embeddings() -> int:
    """Embed every note that doesn't yet have an embedding; idempotent."""
    with tx() as conn:
        rows = conn.execute(
            "SELECT id, title, description FROM notes WHERE embedding IS NULL"
        ).fetchall()

    filled = 0
    for r in rows:
        blob, ts = _try_embed(r["title"], r["description"])
        if blob is None:
            logger.warning("Backfill skipped note %d — embedding unavailable", r["id"])
            continue
        with tx() as conn:
            conn.execute(
                "UPDATE notes SET embedding = ?, embedding_updated_at = ? WHERE id = ?",
                (blob, ts, r["id"]),
            )
        filled += 1
    if filled:
        logger.info("Backfilled embeddings for %d note(s)", filled)
    return filled
