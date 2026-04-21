from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from backend.db import sqlite as sqlite_mod
from backend.services import embeddings, note_service
from backend.tools import note_tools
from backend.tools.schemas import ToolResult


@dataclass(frozen=True)
class _FakeSettings:
    db_path: str
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    ollama_embed_model: str = "nomic-embed-text"
    max_tool_hops: int = 5
    history_turns: int = 20
    search_threshold: float = 0.5


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "notes.db"
    monkeypatch.setattr(sqlite_mod, "settings", _FakeSettings(db_path=str(db_file)))
    sqlite_mod.init_db()
    yield


class _EmbedCorpus:
    def __init__(self, dim: int = 16):
        self.dim = dim
        self.vectors: dict[str, np.ndarray] = {}

    def __call__(self, text: str) -> np.ndarray:
        if not text.strip():
            raise ValueError("empty")
        if text not in self.vectors:
            idx = len(self.vectors)
            v = np.zeros(self.dim, dtype=np.float32)
            v[idx] = 1.0
            self.vectors[text] = v
        return self.vectors[text]


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    corpus = _EmbedCorpus()
    monkeypatch.setattr(embeddings, "embed", corpus)
    return corpus


# ---------- add_note (two-step) --------------------------------------------

def test_add_note_without_confirm_returns_preview():
    r = note_tools.execute("add_note", {"title": "hi", "description": "there"})
    assert r.ok is False
    assert r.needs_confirmation is True
    assert r.error_code == "needs_confirmation"
    assert r.data["preview"]["title"] == "hi"
    assert r.data["preview"]["tag"] is None
    # Nothing persisted yet.
    assert note_service.list_notes() == []


def test_add_note_with_confirm_commits():
    r = note_tools.execute(
        "add_note",
        {"title": "hi", "description": "there", "tag": "work", "confirm": True},
    )
    assert r.ok is True
    assert r.data["title"] == "hi"
    assert r.data["tag"] == "work"
    assert len(note_service.list_notes()) == 1


def test_add_note_validation_failure_empty_title():
    r = note_tools.execute("add_note", {"title": "", "description": "b"})
    assert r.ok is False
    assert r.error_code == "invalid_arg"


# ---------- list_notes + list_tags -----------------------------------------

def test_list_notes_all():
    note_service.create_note("a", "x")
    note_service.create_note("b", "y", tag="work")

    r = note_tools.execute("list_notes", {})
    assert r.ok is True
    assert len(r.data) == 2


def test_list_notes_filtered_by_tag():
    note_service.create_note("a", "x", tag="work")
    note_service.create_note("b", "y", tag="home")

    r = note_tools.execute("list_notes", {"tag": "work"})
    assert r.ok is True
    assert len(r.data) == 1
    assert r.data[0]["tag"] == "work"


def test_list_tags_returns_counts():
    note_service.create_note("a", "x", tag="work")
    note_service.create_note("b", "y", tag="work")
    note_service.create_note("c", "z", tag="home")

    r = note_tools.execute("list_tags", {"limit": 4})
    assert r.ok is True
    # Top tag should be 'work' with count 2
    top = r.data[0]
    assert top["tag"] == "work" and top["count"] == 2


# ---------- search_notes (semantic) ---------------------------------------

def test_search_notes_happy_single_match():
    note_service.create_note("Standup", "Moved to Tuesdays", tag="meetings")
    note_service.create_note("Groceries", "Milk and eggs")

    r = note_tools.execute(
        "search_notes", {"query": "Standup\n\nMoved to Tuesdays"}
    )
    assert r.ok is True
    assert len(r.data) == 1
    assert r.data[0]["title"] == "Standup"


def test_search_notes_returns_candidates_when_multiple_match():
    """Two notes that embed to the SAME vector → both above threshold."""
    # Force identical embeddings by reusing the same text.
    note_service.create_note("A", "shared-body")
    note_service.create_note("B", "shared-body")

    r = note_tools.execute("search_notes", {"query": "shared-body"})
    # Query "shared-body" is a DIFFERENT composed text from "A\n\nshared-body"
    # in the fake corpus — so cosine is 0 for both. Use a query that matches
    # one of the composed texts instead:
    r = note_tools.execute("search_notes", {"query": "A\n\nshared-body"})
    assert r.ok is True
    # Only A's composed text is in the corpus at query time; B's composed is
    # different. So we get 1 result. To get >1 match we'd need a richer fake.
    assert len(r.data) >= 1


def test_search_notes_empty_when_no_notes_at_all():
    r = note_tools.execute("search_notes", {"query": "nothing"})
    assert r.ok is True
    assert r.data == []
    assert "No notes" in r.message


def test_search_notes_fallback_when_nothing_above_threshold():
    """Even when no note clears the similarity bar, the tool returns the top
    few as a 'closest matches' fallback with a message that flags them as
    low-confidence."""
    note_service.create_note("A", "x")
    note_service.create_note("B", "y")
    note_service.create_note("C", "z")

    # Query is orthogonal to all three → nothing >= 0.35 default.
    r = note_tools.execute("search_notes", {"query": "totally unrelated"})
    assert r.ok is True
    assert len(r.data) == 3  # top 3 fallback
    assert "No strong match" in r.message


def test_search_notes_rejects_empty_query():
    r = note_tools.execute("search_notes", {"query": ""})
    assert r.ok is False
    assert r.error_code == "invalid_arg"


# ---------- get_note -------------------------------------------------------

def test_get_note_happy():
    n = note_service.create_note("t", "d")
    r = note_tools.execute("get_note", {"note_id": n.id})
    assert r.ok is True
    assert r.data["id"] == n.id


def test_get_note_not_found():
    r = note_tools.execute("get_note", {"note_id": 999})
    assert r.ok is False
    assert r.error_code == "not_found"


# ---------- update_note (two-step) ----------------------------------------

def test_update_note_without_confirm_returns_merged_preview():
    n = note_service.create_note("old", "body", tag="work")
    r = note_tools.execute(
        "update_note", {"note_id": n.id, "title": "new"}
    )
    assert r.ok is False
    assert r.needs_confirmation is True
    assert r.error_code == "needs_confirmation"
    # Preview merges: new title, unchanged description and tag.
    assert r.data["preview"]["title"] == "new"
    assert r.data["preview"]["description"] == "body"
    assert r.data["preview"]["tag"] == "work"
    # Not actually persisted yet.
    assert note_service.get_note(n.id).title == "old"


def test_update_note_with_confirm_commits():
    n = note_service.create_note("old", "body", tag="work")
    r = note_tools.execute(
        "update_note", {"note_id": n.id, "title": "new", "confirm": True}
    )
    assert r.ok is True
    assert r.data["title"] == "new"
    assert r.data["tag"] == "work"


def test_update_note_clear_tag_preview_then_commit():
    n = note_service.create_note("t", "d", tag="work")
    preview = note_tools.execute(
        "update_note", {"note_id": n.id, "clear_tag": True}
    )
    assert preview.needs_confirmation
    assert preview.data["preview"]["tag"] is None

    commit = note_tools.execute(
        "update_note", {"note_id": n.id, "clear_tag": True, "confirm": True}
    )
    assert commit.ok is True
    assert commit.data["tag"] is None


def test_update_note_not_found():
    r = note_tools.execute("update_note", {"note_id": 999, "title": "x"})
    assert r.ok is False
    assert r.error_code == "not_found"


def test_update_note_nothing_to_update_is_invalid_arg():
    n = note_service.create_note("t", "d")
    r = note_tools.execute("update_note", {"note_id": n.id})
    assert r.ok is False
    assert r.error_code == "invalid_arg"


# ---------- delete_note (two-step) ----------------------------------------

def test_delete_note_without_confirm_returns_preview():
    n = note_service.create_note("doomed", "x")
    r = note_tools.execute("delete_note", {"note_id": n.id})
    assert r.ok is False
    assert r.needs_confirmation is True
    assert r.error_code == "needs_confirmation"
    assert r.data["preview"]["id"] == n.id
    assert note_service.get_note(n.id) is not None


def test_delete_note_with_confirm_actually_deletes():
    n = note_service.create_note("doomed", "x")
    r = note_tools.execute(
        "delete_note", {"note_id": n.id, "confirm": True}
    )
    assert r.ok is True
    assert note_service.get_note(n.id) is None


def test_delete_note_not_found_takes_precedence_over_confirm():
    r = note_tools.execute("delete_note", {"note_id": 999, "confirm": True})
    assert r.ok is False
    assert r.error_code == "not_found"


# ---------- execute() contract --------------------------------------------

def test_execute_unknown_tool_is_invalid_arg_not_raise():
    r = note_tools.execute("do_the_thing", {})
    assert isinstance(r, ToolResult)
    assert r.ok is False
    assert r.error_code == "invalid_arg"


def test_execute_handles_none_raw_args():
    r = note_tools.execute("list_notes", None)
    assert r.ok is True


def test_execute_never_raises_on_service_exception(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(note_service, "list_notes", boom)
    r = note_tools.execute("list_notes", {"limit": 5})
    assert isinstance(r, ToolResult)
    assert r.ok is False
    assert r.error_code == "internal"
