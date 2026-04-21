from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.db import sqlite as sqlite_mod
from backend.services import note_service
from backend.tools import note_tools
from backend.tools.schemas import ToolResult


@dataclass(frozen=True)
class _FakeSettings:
    db_path: str
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    max_tool_hops: int = 5
    history_turns: int = 20


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "notes.db"
    monkeypatch.setattr(sqlite_mod, "settings", _FakeSettings(db_path=str(db_file)))
    sqlite_mod.init_db()
    yield str(db_file)


# ---------- add_note --------------------------------------------------------

def test_add_note_happy():
    r = note_tools.execute("add_note", {"title": "hi", "body": "there", "tags": ["x"]})
    assert r.ok is True
    assert r.data["title"] == "hi"
    assert r.data["tags"] == ["x"]


def test_add_note_validation_failure_empty_title():
    r = note_tools.execute("add_note", {"title": "", "body": "b"})
    assert r.ok is False
    assert r.error_code == "invalid_arg"


# ---------- search_notes ----------------------------------------------------

def test_search_notes_happy():
    note_service.create_note("deadline reminder", "ship friday", ["work"])
    note_service.create_note("grocery", "milk eggs", ["home"])
    r = note_tools.execute("search_notes", {"query": "deadline"})
    assert r.ok is True
    assert len(r.data) == 1
    assert r.data[0]["title"] == "deadline reminder"


def test_search_notes_empty_is_ok_with_empty_data():
    r = note_tools.execute("search_notes", {"query": "nothing_here"})
    assert r.ok is True
    assert r.data == []


def test_search_notes_flags_ambiguity_when_limit_one_but_many_match():
    note_service.create_note("meeting monday", "a", ["work"])
    note_service.create_note("meeting tuesday", "b", ["work"])
    r = note_tools.execute("search_notes", {"query": "meeting", "limit": 1})
    assert r.ok is False
    assert r.error_code == "ambiguous"
    assert r.candidates is not None and len(r.candidates) == 2


def test_search_notes_limit_one_unique_match_is_happy():
    note_service.create_note("only", "the single match", [])
    note_service.create_note("other", "unrelated content", [])
    r = note_tools.execute("search_notes", {"query": "single", "limit": 1})
    assert r.ok is True
    assert len(r.data) == 1


# ---------- get_note --------------------------------------------------------

def test_get_note_happy():
    n = note_service.create_note("t", "b", [])
    r = note_tools.execute("get_note", {"note_id": n.id})
    assert r.ok is True
    assert r.data["id"] == n.id


def test_get_note_not_found():
    r = note_tools.execute("get_note", {"note_id": 999})
    assert r.ok is False
    assert r.error_code == "not_found"


# ---------- update_note -----------------------------------------------------

def test_update_note_happy():
    n = note_service.create_note("old", "body", [])
    r = note_tools.execute(
        "update_note", {"note_id": n.id, "title": "new", "tags": ["x"]}
    )
    assert r.ok is True
    assert r.data["title"] == "new"
    assert r.data["tags"] == ["x"]


def test_update_note_not_found():
    r = note_tools.execute("update_note", {"note_id": 999, "title": "x"})
    assert r.ok is False
    assert r.error_code == "not_found"


def test_update_note_nothing_to_update_is_invalid_arg():
    n = note_service.create_note("t", "b", [])
    r = note_tools.execute("update_note", {"note_id": n.id})
    assert r.ok is False
    assert r.error_code == "invalid_arg"


# ---------- delete_note (two-step) ------------------------------------------

def test_delete_note_without_confirm_returns_preview():
    n = note_service.create_note("doomed", "x", [])
    r = note_tools.execute("delete_note", {"note_id": n.id})
    assert r.ok is False
    assert r.needs_confirmation is True
    assert r.error_code == "needs_confirmation"
    assert r.data["preview"]["id"] == n.id
    # And the note is still there.
    assert note_service.get_note(n.id) is not None


def test_delete_note_with_confirm_actually_deletes():
    n = note_service.create_note("doomed", "x", [])
    r = note_tools.execute("delete_note", {"note_id": n.id, "confirm": True})
    assert r.ok is True
    assert note_service.get_note(n.id) is None


def test_delete_note_not_found_takes_precedence_over_confirm():
    r = note_tools.execute("delete_note", {"note_id": 999, "confirm": True})
    assert r.ok is False
    assert r.error_code == "not_found"


# ---------- list_recent -----------------------------------------------------

def test_list_recent_happy():
    a = note_service.create_note("a", "1", [])
    b = note_service.create_note("b", "2", [])
    r = note_tools.execute("list_recent", {"limit": 5})
    assert r.ok is True
    ids = [row["id"] for row in r.data]
    # b is most recent (created second)
    assert ids[0] == b.id
    assert a.id in ids


def test_list_recent_invalid_limit():
    r = note_tools.execute("list_recent", {"limit": 0})
    assert r.ok is False
    assert r.error_code == "invalid_arg"


# ---------- summarize_notes -------------------------------------------------

def test_summarize_notes_happy():
    n1 = note_service.create_note("a", "body a", [])
    n2 = note_service.create_note("b", "body b", [])
    r = note_tools.execute("summarize_notes", {"note_ids": [n1.id, n2.id]})
    assert r.ok is True
    assert len(r.data) == 2


def test_summarize_notes_all_missing_is_not_found():
    r = note_tools.execute("summarize_notes", {"note_ids": [999, 1000]})
    assert r.ok is False
    assert r.error_code == "not_found"


def test_summarize_notes_partial_missing_still_ok_with_note():
    n = note_service.create_note("a", "b", [])
    r = note_tools.execute("summarize_notes", {"note_ids": [n.id, 999]})
    assert r.ok is True
    assert len(r.data) == 1
    assert "999" in r.message  # surfaces the missing id


# ---------- execute() contract ---------------------------------------------

def test_execute_unknown_tool_is_invalid_arg_not_raise():
    r = note_tools.execute("do_the_thing", {})
    assert isinstance(r, ToolResult)
    assert r.ok is False
    assert r.error_code == "invalid_arg"


def test_execute_handles_none_raw_args_gracefully():
    r = note_tools.execute("list_recent", None)
    assert r.ok is True


def test_execute_never_raises_on_service_exception(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(note_service, "list_recent", boom)
    r = note_tools.execute("list_recent", {"limit": 5})
    assert isinstance(r, ToolResult)
    assert r.ok is False
    assert r.error_code == "internal"
    assert "db is on fire" in r.message
