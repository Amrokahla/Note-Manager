from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from backend.db.sqlite import tx
from backend.services.models import Note, NoteSummary


def normalize_tag(t: str) -> str:
    return t.strip().lstrip("#").lower()


def _normalize_tags(tags: Iterable[str] | None) -> list[str]:
    if not tags:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        n = normalize_tag(raw)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_tags(conn: sqlite3.Connection, note_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM tags WHERE note_id = ? ORDER BY tag",
        (note_id,),
    ).fetchall()
    return [r["tag"] for r in rows]


def _insert_tags(conn: sqlite3.Connection, note_id: int, tags: list[str]) -> None:
    if not tags:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO tags(note_id, tag) VALUES (?, ?)",
        [(note_id, t) for t in tags],
    )


def _row_to_note(row: sqlite3.Row, tags: list[str]) -> Note:
    return Note(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        tags=tags,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_summary(row: sqlite3.Row, tags: list[str]) -> NoteSummary:
    return NoteSummary(
        id=row["id"],
        title=row["title"],
        snippet=row["snippet"],
        tags=tags,
        updated_at=row["updated_at"],
    )


def create_note(title: str, body: str, tags: list[str] | None = None) -> Note:
    now = _utc_now_iso()
    norm = _normalize_tags(tags)
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO notes(title, body, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (title, body, now, now),
        )
        note_id = int(cur.lastrowid)
        _insert_tags(conn, note_id, norm)
        row = conn.execute(
            "SELECT id, title, body, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        stored_tags = _fetch_tags(conn, note_id)
    return _row_to_note(row, stored_tags)


def get_note(note_id: int) -> Note | None:
    with tx() as conn:
        row = conn.execute(
            "SELECT id, title, body, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        if row is None:
            return None
        tags = _fetch_tags(conn, note_id)
    return _row_to_note(row, tags)


def update_note(
    note_id: int,
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
) -> Note | None:
    with tx() as conn:
        existing = conn.execute(
            "SELECT id FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        if existing is None:
            return None

        set_clauses: list[str] = []
        args: list = []
        if title is not None:
            set_clauses.append("title = ?")
            args.append(title)
        if body is not None:
            set_clauses.append("body = ?")
            args.append(body)

        anything_changed = bool(set_clauses) or tags is not None
        if anything_changed:
            set_clauses.append("updated_at = ?")
            args.append(_utc_now_iso())
            args.append(note_id)
            conn.execute(
                f"UPDATE notes SET {', '.join(set_clauses)} WHERE id = ?",
                args,
            )

        if tags is not None:
            norm = _normalize_tags(tags)
            conn.execute("DELETE FROM tags WHERE note_id = ?", (note_id,))
            _insert_tags(conn, note_id, norm)

        row = conn.execute(
            "SELECT id, title, body, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        final_tags = _fetch_tags(conn, note_id)
    return _row_to_note(row, final_tags)


def delete_note(note_id: int) -> bool:
    with tx() as conn:
        cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return cur.rowcount > 0


def list_recent(limit: int = 5) -> list[NoteSummary]:
    with tx() as conn:
        rows = conn.execute(
            """
            SELECT id, title, substr(body, 1, 200) AS snippet, updated_at
            FROM notes
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_row_to_summary(r, _fetch_tags(conn, r["id"])) for r in rows]


def search_notes(
    query: str | None = None,
    tags: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 10,
) -> list[NoteSummary]:
    norm_tags = _normalize_tags(tags)

    where: list[str] = []
    args: list = []
    joins: list[str] = []

    if query:
        joins.append("JOIN notes_fts f ON f.rowid = n.id")
        where.append("notes_fts MATCH ?")
        args.append(_to_fts_query(query))

    if norm_tags:
        joins.append("JOIN tags t ON t.note_id = n.id")
        placeholders = ",".join("?" * len(norm_tags))
        where.append(f"t.tag IN ({placeholders})")
        args.extend(norm_tags)

    if date_from is not None:
        where.append("n.created_at >= ?")
        args.append(date_from.isoformat())
    if date_to is not None:
        where.append("n.created_at <= ?")
        args.append(date_to.isoformat())

    join_clause = " ".join(joins)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT DISTINCT n.id, n.title, substr(n.body, 1, 200) AS snippet, n.updated_at
        FROM notes n
        {join_clause}
        {where_clause}
        ORDER BY n.updated_at DESC
        LIMIT ?
    """
    args.append(limit)

    with tx() as conn:
        rows = conn.execute(sql, args).fetchall()
        return [_row_to_summary(r, _fetch_tags(conn, r["id"])) for r in rows]


def _to_fts_query(query: str) -> str:
    # Quote each token so FTS5 treats them as literal terms (escapes punctuation
    # that would otherwise be interpreted as FTS operators like - + * " : ( ) ).
    tokens = [t for t in query.split() if t]
    if not tokens:
        return '""'
    safe = [f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens]
    return " ".join(safe)
