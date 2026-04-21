from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from backend.db import sqlite as sqlite_mod


@dataclass(frozen=True)
class _FakeSettings:
    db_path: str
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    max_tool_hops: int = 5
    history_turns: int = 20


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Point the DB layer at a per-test sqlite file and run migrations."""
    db_file = tmp_path / "notes.db"
    monkeypatch.setattr(sqlite_mod, "settings", _FakeSettings(db_path=str(db_file)))
    sqlite_mod.init_db()
    yield str(db_file)


def test_normalize_tag_helper():
    from backend.services.note_service import normalize_tag

    assert normalize_tag("#Foo") == "foo"
    assert normalize_tag("  BAR ") == "bar"
    assert normalize_tag("##baz") == "baz"
    assert normalize_tag("Already-lower") == "already-lower"


def test_create_dedupes_and_normalizes_tags():
    from backend.services import note_service

    note = note_service.create_note("t", "b", ["#Foo", "FOO", " foo ", "bar"])
    assert sorted(note.tags) == ["bar", "foo"]


def test_create_and_get_note_roundtrip():
    from backend.services import note_service

    note = note_service.create_note("hello", "world body", ["work"])
    assert note.id > 0
    assert note.title == "hello"
    assert note.body == "world body"
    assert note.tags == ["work"]
    assert note.created_at == note.updated_at

    fetched = note_service.get_note(note.id)
    assert fetched is not None
    assert fetched.id == note.id
    assert fetched.tags == ["work"]


def test_get_note_missing_returns_none():
    from backend.services import note_service

    assert note_service.get_note(999) is None


def test_update_note_patches_fields_and_touches_updated_at():
    from backend.services import note_service

    note = note_service.create_note("old", "old body", ["a"])
    # Sleep a hair to guarantee updated_at differs at ISO-string resolution.
    time.sleep(0.01)

    updated = note_service.update_note(note.id, title="new", tags=["b", "c"])
    assert updated is not None
    assert updated.title == "new"
    assert updated.body == "old body"
    assert sorted(updated.tags) == ["b", "c"]
    assert updated.updated_at > note.updated_at


def test_update_note_missing_returns_none():
    from backend.services import note_service

    assert note_service.update_note(999, title="x") is None


def test_delete_note():
    from backend.services import note_service

    note = note_service.create_note("doomed", "bye", [])
    assert note_service.delete_note(note.id) is True
    assert note_service.get_note(note.id) is None
    assert note_service.delete_note(note.id) is False  # idempotent on missing


def test_delete_cascades_tags():
    from backend.services import note_service

    note = note_service.create_note("t", "b", ["x"])
    note_service.delete_note(note.id)
    with sqlite_mod.tx() as conn:
        rows = conn.execute("SELECT COUNT(*) AS c FROM tags WHERE note_id = ?", (note.id,)).fetchone()
    assert rows["c"] == 0


def test_search_by_keyword_hits_fts():
    from backend.services import note_service

    a = note_service.create_note("deadline reminder", "ship the thing by friday", ["work"])
    note_service.create_note("grocery list", "milk eggs bread", ["home"])

    results = note_service.search_notes(query="deadline")
    assert [r.id for r in results] == [a.id]
    assert results[0].snippet.startswith("ship the thing")


def test_search_keyword_matches_body_terms():
    from backend.services import note_service

    target = note_service.create_note("random", "meeting moved to tuesday", [])
    note_service.create_note("unrelated", "something else entirely", [])

    results = note_service.search_notes(query="tuesday")
    assert [r.id for r in results] == [target.id]


def test_search_by_tag():
    from backend.services import note_service

    urgent1 = note_service.create_note("a", "body1", ["urgent"])
    urgent2 = note_service.create_note("b", "body2", ["Urgent", "work"])
    note_service.create_note("c", "body3", ["home"])

    results = note_service.search_notes(tags=["urgent"])
    ids = sorted(r.id for r in results)
    assert ids == sorted([urgent1.id, urgent2.id])


def test_search_by_date_range():
    from backend.services import note_service

    n1 = note_service.create_note("a", "x", [])
    # Force n1 into the past by hand — our service always writes "now".
    with sqlite_mod.tx() as conn:
        past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn.execute(
            "UPDATE notes SET created_at = ?, updated_at = ? WHERE id = ?",
            (past, past, n1.id),
        )

    n2 = note_service.create_note("b", "y", [])

    since = (datetime.now(timezone.utc) - timedelta(days=1))
    results = note_service.search_notes(date_from=since)
    assert [r.id for r in results] == [n2.id]

    older = note_service.search_notes(
        date_from=datetime.now(timezone.utc) - timedelta(days=30),
        date_to=datetime.now(timezone.utc) - timedelta(days=5),
    )
    assert [r.id for r in older] == [n1.id]


def test_search_returns_empty_when_no_match():
    from backend.services import note_service

    note_service.create_note("a", "b", ["x"])
    assert note_service.search_notes(query="nonexistentword") == []
    assert note_service.search_notes(tags=["does-not-exist"]) == []


def test_list_recent_orders_by_updated_at_desc():
    from backend.services import note_service

    a = note_service.create_note("a", "1", [])
    time.sleep(0.01)
    b = note_service.create_note("b", "2", [])
    time.sleep(0.01)
    c = note_service.create_note("c", "3", [])

    recent = note_service.list_recent(limit=5)
    assert [r.id for r in recent] == [c.id, b.id, a.id]
