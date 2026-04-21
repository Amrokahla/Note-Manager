from __future__ import annotations

from backend.agent.conversation_state import (
    SessionState,
    SessionStore,
    build_context_line,
    remember_referenced,
)
from backend.config import settings
from backend.tools.schemas import ToolResult


# ---------- SessionStore ----------------------------------------------------

def test_store_creates_session_on_first_get():
    store = SessionStore()
    s = store.get("alice")
    assert isinstance(s, SessionState)
    assert s.session_id == "alice"


def test_store_returns_same_instance_on_repeat_get():
    store = SessionStore()
    s1 = store.get("alice")
    s1.messages.append({"role": "user", "content": "hi"})
    s2 = store.get("alice")
    assert s2 is s1
    assert list(s2.messages) == [{"role": "user", "content": "hi"}]


def test_store_isolates_sessions():
    store = SessionStore()
    a = store.get("alice")
    b = store.get("bob")
    assert a is not b
    a.last_referenced_note_ids = [1]
    assert b.last_referenced_note_ids == []


def test_store_reset_drops_session():
    store = SessionStore()
    store.get("alice").last_referenced_note_ids = [7]
    store.reset("alice")
    fresh = store.get("alice")
    assert fresh.last_referenced_note_ids == []


# ---------- Message deque truncation ----------------------------------------

def test_messages_deque_truncates_at_maxlen():
    state = SessionState(session_id="s")
    maxlen = settings.history_turns * 2
    for i in range(maxlen + 10):
        state.messages.append({"role": "user", "content": f"m{i}"})
    assert len(state.messages) == maxlen
    # Oldest messages were dropped — the first surviving one is m10.
    assert state.messages[0]["content"] == "m10"


# ---------- remember_referenced --------------------------------------------

def test_remember_from_list_of_summaries():
    state = SessionState(session_id="s")
    result = ToolResult(
        ok=True,
        message="Found 2 note(s).",
        data=[
            {"id": 3, "title": "a"},
            {"id": 5, "title": "b"},
        ],
    )
    remember_referenced(state, result)
    assert state.last_referenced_note_ids == [3, 5]


def test_remember_from_single_note_dict():
    state = SessionState(session_id="s")
    result = ToolResult(ok=True, message="Fetched.", data={"id": 42, "title": "z"})
    remember_referenced(state, result)
    assert state.last_referenced_note_ids == [42]


def test_remember_from_delete_preview_wrapper():
    """delete_note returns data={"preview": {"id": N, ...}} on its first hop."""
    state = SessionState(session_id="s")
    result = ToolResult(
        ok=False,
        needs_confirmation=True,
        error_code="needs_confirmation",
        message="About to delete...",
        data={"preview": {"id": 9, "title": "doomed"}},
    )
    remember_referenced(state, result)
    assert state.last_referenced_note_ids == [9]


def test_remember_dedupes_ids():
    state = SessionState(session_id="s")
    result = ToolResult(
        ok=True,
        message="weird",
        data=[{"id": 1}, {"id": 2}, {"id": 1}],
    )
    remember_referenced(state, result)
    assert state.last_referenced_note_ids == [1, 2]


def test_remember_preserves_prior_ids_when_empty_result():
    state = SessionState(session_id="s", last_referenced_note_ids=[11])
    result = ToolResult(ok=True, message="No notes matched.", data=[])
    remember_referenced(state, result)
    assert state.last_referenced_note_ids == [11]


def test_remember_preserves_prior_ids_when_data_is_none():
    state = SessionState(session_id="s", last_referenced_note_ids=[11])
    result = ToolResult(ok=False, error_code="not_found", message="nope")
    remember_referenced(state, result)
    assert state.last_referenced_note_ids == [11]


def test_remember_ignores_dict_rows_without_int_id():
    state = SessionState(session_id="s")
    result = ToolResult(
        ok=True,
        message="weird",
        data=[{"title": "no id"}, {"id": "not-an-int"}, {"id": 7}],
    )
    remember_referenced(state, result)
    assert state.last_referenced_note_ids == [7]


# ---------- build_context_line ---------------------------------------------

def test_context_line_none_for_fresh_state():
    state = SessionState(session_id="s")
    assert build_context_line(state) is None


def test_context_line_mentions_ids_and_primary():
    state = SessionState(session_id="s", last_referenced_note_ids=[17, 18])
    line = build_context_line(state)
    assert line is not None
    assert line.startswith("(context)")
    assert "[17, 18]" in line
    assert "17" in line
    assert "that note" in line


def test_context_line_mentions_pending_confirmation():
    state = SessionState(
        session_id="s",
        pending_confirmation={"tool": "delete_note", "args": {"note_id": 9}},
    )
    line = build_context_line(state)
    assert line is not None
    assert "delete_note" in line
    assert "confirm=true" in line
    # Context now covers modification in addition to yes/no.
    assert "Modification" in line or "modify" in line.lower()


def test_context_line_combines_ids_and_pending_confirmation():
    state = SessionState(
        session_id="s",
        last_referenced_note_ids=[9],
        pending_confirmation={"tool": "delete_note", "args": {"note_id": 9}},
    )
    line = build_context_line(state)
    assert line is not None
    assert "[9]" in line
    assert "delete_note" in line


# ---------- Multi-turn reference scaffolding (DoD §6.4) --------------------

def test_add_then_that_note_resolves_via_state():
    """Simulates the inputs the orchestrator will feed after an add_note turn.
    We can't assert the LLM's update_note(note_id=...) call here (that needs
    Phase 7 + a real model), but we *can* verify that after a create the
    context line carries the right id — which is the signal the LLM uses."""
    state = SessionState(session_id="s")

    add_result = ToolResult(
        ok=True,
        message="Created note #42.",
        data={"id": 42, "title": "standup", "body": "moved to tuesdays", "tags": ["meetings"]},
    )
    remember_referenced(state, add_result)

    line = build_context_line(state)
    assert line is not None
    assert "42" in line
    assert '"that note"' in line
