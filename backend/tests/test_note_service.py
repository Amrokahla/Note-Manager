from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from backend.db import sqlite as sqlite_mod
from backend.services import embeddings


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
    yield str(db_file)


class _EmbedCorpus:
    """A tiny deterministic 'embedding' for tests: maps each distinct text
    to a fresh unit basis vector. Different texts → orthogonal vectors
    (cosine 0). Same text → identical vector (cosine 1). Makes semantic
    search assertions trivial."""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.vectors: dict[str, np.ndarray] = {}

    def __call__(self, text: str) -> np.ndarray:
        if not text.strip():
            raise ValueError("empty")
        if text not in self.vectors:
            idx = len(self.vectors)
            if idx >= self.dim:
                raise RuntimeError("corpus exhausted — bump dim")
            v = np.zeros(self.dim, dtype=np.float32)
            v[idx] = 1.0
            self.vectors[text] = v
        return self.vectors[text]


@pytest.fixture
def fake_embed(monkeypatch):
    corpus = _EmbedCorpus(dim=16)
    monkeypatch.setattr(embeddings, "embed", corpus)
    return corpus


# ---------- Tag normalization ----------------------------------------------

def test_normalize_tag():
    from backend.services.note_service import normalize_tag

    assert normalize_tag("#Meetings") == "meetings"
    assert normalize_tag("  WORK ") == "work"
    assert normalize_tag(None) is None
    assert normalize_tag("   ") is None
    assert normalize_tag("##foo") == "foo"


# ---------- CRUD basics ----------------------------------------------------

def test_create_and_get_note(fake_embed):
    from backend.services import note_service

    n = note_service.create_note("Standup", "Moved to Tuesdays", tag="meetings")
    assert n.id > 0
    assert n.title == "Standup"
    assert n.description == "Moved to Tuesdays"
    assert n.tag == "meetings"

    fetched = note_service.get_note(n.id)
    assert fetched is not None
    assert fetched.title == "Standup"


def test_create_without_tag(fake_embed):
    from backend.services import note_service

    n = note_service.create_note("Idea", "Random thought")
    assert n.tag is None


def test_get_note_missing_returns_none(fake_embed):
    from backend.services import note_service

    assert note_service.get_note(999) is None


def test_update_note_patches_fields(fake_embed):
    from backend.services import note_service

    n = note_service.create_note("old title", "old body", tag="work")
    updated = note_service.update_note(n.id, title="new title", description="new body")
    assert updated.title == "new title"
    assert updated.description == "new body"
    assert updated.tag == "work"  # unchanged


def test_update_note_can_change_tag_only(fake_embed):
    from backend.services import note_service

    n = note_service.create_note("t", "d", tag="work")
    updated = note_service.update_note(n.id, tag="personal")
    assert updated.tag == "personal"


def test_update_note_can_clear_tag(fake_embed):
    from backend.services import note_service

    n = note_service.create_note("t", "d", tag="work")
    updated = note_service.update_note(n.id, clear_tag=True)
    assert updated.tag is None


def test_update_note_missing_returns_none(fake_embed):
    from backend.services import note_service

    assert note_service.update_note(999, title="x") is None


def test_delete_note(fake_embed):
    from backend.services import note_service

    n = note_service.create_note("doomed", "x")
    assert note_service.delete_note(n.id) is True
    assert note_service.get_note(n.id) is None
    assert note_service.delete_note(n.id) is False


# ---------- list_notes + list_tags -----------------------------------------

def test_list_notes_orders_by_updated_desc(fake_embed):
    from backend.services import note_service
    import time

    a = note_service.create_note("a", "1")
    time.sleep(0.01)
    b = note_service.create_note("b", "2")
    time.sleep(0.01)
    c = note_service.create_note("c", "3")

    results = note_service.list_notes(limit=5)
    assert [r.id for r in results] == [c.id, b.id, a.id]


def test_list_notes_filter_by_tag(fake_embed):
    from backend.services import note_service

    a = note_service.create_note("a", "1", tag="work")
    b = note_service.create_note("b", "2", tag="home")
    c = note_service.create_note("c", "3", tag="work")

    work = note_service.list_notes(tag="work")
    ids = sorted(r.id for r in work)
    assert ids == sorted([a.id, c.id])

    home = note_service.list_notes(tag="home")
    assert [r.id for r in home] == [b.id]


def test_list_notes_filter_normalizes_tag_case(fake_embed):
    from backend.services import note_service

    note_service.create_note("x", "y", tag="work")
    results = note_service.list_notes(tag="WORK")
    assert len(results) == 1


def test_list_tags_returns_top_n_by_count(fake_embed):
    from backend.services import note_service

    note_service.create_note("a", "1", tag="work")
    note_service.create_note("b", "2", tag="work")
    note_service.create_note("c", "3", tag="home")
    note_service.create_note("d", "4", tag="urgent")
    note_service.create_note("e", "5")  # no tag

    tags = note_service.list_tags(limit=4)
    # "work" has 2, the rest have 1 — "work" must come first
    assert tags[0].tag == "work"
    assert tags[0].count == 2
    # Returns at most 4 distinct tagged buckets; the rest alphabetize
    assert len(tags) == 3
    assert {t.tag for t in tags} == {"work", "home", "urgent"}


# ---------- search_semantic ------------------------------------------------

def test_search_semantic_ranks_exact_match_first(fake_embed):
    from backend.services import note_service

    a = note_service.create_note("Standup", "Moved to Tuesdays", tag="meetings")
    note_service.create_note("Groceries", "Milk and eggs", tag="home")
    note_service.create_note("Birthday", "Sarah turns 30", tag="personal")

    # Query re-uses the exact composed text for note a → cosine = 1.0
    results, above = note_service.search_semantic("Standup\n\nMoved to Tuesdays", limit=5)
    assert above is True
    assert len(results) >= 1
    assert results[0].id == a.id
    assert results[0].similarity == pytest.approx(1.0)


def test_search_semantic_falls_back_when_nothing_above_threshold(fake_embed):
    from backend.services import note_service

    note_service.create_note("A", "x")
    note_service.create_note("B", "y")
    note_service.create_note("C", "z")
    # Query is orthogonal to all three (cosine = 0) → nothing >= threshold.
    # The service still returns the top fallback_limit (default 3) as
    # low-confidence candidates with above_threshold=False.
    results, above = note_service.search_semantic("totally unrelated query")
    assert above is False
    assert len(results) == 3


def test_search_semantic_returns_empty_when_no_notes(fake_embed):
    from backend.services import note_service

    results, above = note_service.search_semantic("anything")
    assert results == []
    assert above is False


def test_search_semantic_skips_rows_without_embedding(fake_embed, monkeypatch):
    """If embedding failed on write, the note shouldn't appear in search."""
    from backend.services import note_service

    note_service.create_note("A", "present")

    def broken(_text):
        raise RuntimeError("Ollama down")

    monkeypatch.setattr(embeddings, "embed", broken)
    note_service.create_note("B", "missing embedding")

    monkeypatch.setattr(embeddings, "embed", fake_embed)

    results, above = note_service.search_semantic("A\n\npresent")
    ids = [r.id for r in results]
    assert ids == [1]  # only A has an embedding
    assert above is True


# ---------- Embedding re-computation on update -----------------------------

def test_update_recomputes_embedding_when_text_changes(fake_embed):
    from backend.services import note_service

    n = note_service.create_note("OldTitle", "old body")
    # Snapshot the embedding timestamp
    before = note_service.get_note(n.id).updated_at

    import time; time.sleep(0.01)

    note_service.update_note(n.id, title="NewTitle", description="new body")
    # The new composed text finds this note with high similarity
    results, above = note_service.search_semantic("NewTitle\n\nnew body", threshold=0.9)
    assert above is True
    assert [r.id for r in results] == [n.id]
    # Old composed text no longer matches above the high bar (re-embedded).
    old_results, old_above = note_service.search_semantic(
        "OldTitle\n\nold body", threshold=0.9
    )
    assert old_above is False


def test_update_tag_only_does_not_reembed(fake_embed):
    """Tag changes don't trigger an embed call — keeps the service cheap."""
    from backend.services import note_service

    n = note_service.create_note("A", "body")
    calls_before = len(fake_embed.vectors)
    note_service.update_note(n.id, tag="work")
    calls_after = len(fake_embed.vectors)
    assert calls_before == calls_after  # no new embeddings computed


# ---------- Backfill -------------------------------------------------------

def test_backfill_fills_missing_embeddings(fake_embed, monkeypatch):
    from backend.services import note_service

    # Make the first two creates fail to embed
    def broken(_t):
        raise RuntimeError("down")

    monkeypatch.setattr(embeddings, "embed", broken)
    note_service.create_note("A", "ax")
    note_service.create_note("B", "bx")

    # Restore
    monkeypatch.setattr(embeddings, "embed", fake_embed)

    filled = note_service.backfill_embeddings()
    assert filled == 2

    # Now search works for them
    results, above = note_service.search_semantic("A\n\nax", threshold=0.9)
    assert above is True
    assert len(results) == 1
