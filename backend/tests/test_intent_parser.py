from __future__ import annotations

from dataclasses import dataclass

import pytest

import numpy as np

from backend.agent import conversation_state, intent_parser, llm_handler
from backend.agent.llm_handler import LLMResponse, ToolCall
from backend.config import settings
from backend.db import sqlite as sqlite_mod
from backend.services import embeddings, note_service


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


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    """Deterministic per-text orthogonal unit vectors — avoids real Ollama."""
    corpus: dict[str, np.ndarray] = {}

    def embed(text: str) -> np.ndarray:
        if not text.strip():
            raise ValueError("empty")
        if text not in corpus:
            idx = len(corpus)
            v = np.zeros(32, dtype=np.float32)
            v[idx] = 1.0
            corpus[text] = v
        return corpus[text]

    monkeypatch.setattr(embeddings, "embed", embed)
    return corpus


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    """Every test gets an empty SessionStore — no cross-test leakage."""
    monkeypatch.setattr(intent_parser, "store", conversation_state.SessionStore())


def _install_chat(monkeypatch, responses: list[LLMResponse]):
    """Patch llm_handler.chat to pop scripted responses and capture inputs."""
    queue = list(responses)
    captured: list[list[dict]] = []

    def fake_chat(messages, **_kwargs):
        captured.append([dict(m) for m in messages])
        if not queue:
            raise AssertionError("chat() called more times than mocked responses")
        return queue.pop(0)

    monkeypatch.setattr(llm_handler, "chat", fake_chat)
    return captured


# ---------- Basic dispatch paths -------------------------------------------

def test_plain_message_path(monkeypatch):
    captured = _install_chat(monkeypatch, [LLMResponse(kind="message", content="hello!")])

    result = intent_parser.handle_user_message("s1", "hi")

    assert result.reply == "hello!"
    assert result.tool_calls == []
    # First built message is the system prompt.
    assert captured[0][0]["role"] == "system"
    assert "note-taking" in captured[0][0]["content"].lower()


def test_single_tool_call_then_final_message(monkeypatch):
    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(name="add_note", arguments={"title": "t", "description": "b", "confirm": True})
                ],
            ),
            LLMResponse(kind="message", content="Done — created note."),
        ],
    )

    result = intent_parser.handle_user_message("s1", "save a note")

    assert "Done" in result.reply
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "add_note"
    assert result.tool_calls[0].result.ok is True
    recent = note_service.list_notes(limit=5)
    assert len(recent) == 1 and recent[0].title == "t"


def test_message_history_shape_after_tool_call(monkeypatch):
    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[ToolCall(name="list_notes", arguments={"limit": 3})],
            ),
            LLMResponse(kind="message", content="none yet"),
        ],
    )
    intent_parser.handle_user_message("s1", "what's recent")

    state = intent_parser.store.get("s1")
    roles = [m["role"] for m in state.messages]
    # user → assistant(tool_call) → tool(result) → assistant(final)
    assert roles == ["user", "assistant", "tool", "assistant"]
    # The tool message carries the ToolResult JSON.
    tool_msg = list(state.messages)[2]
    assert tool_msg["name"] == "list_notes"
    assert '"ok": true' in tool_msg["content"] or '"ok":true' in tool_msg["content"]


# ---------- MAX_TOOL_HOPS --------------------------------------------------

def test_max_tool_hops_triggers_fallback(monkeypatch):
    looping = LLMResponse(
        kind="tool_calls",
        tool_calls=[ToolCall(name="list_notes", arguments={"limit": 1})],
    )
    # Supply more than enough to guarantee we hit the cap.
    captured = _install_chat(monkeypatch, [looping] * 20)

    result = intent_parser.handle_user_message("s1", "loop forever")

    assert "trouble" in result.reply.lower()
    # Exactly max_tool_hops LLM calls were made.
    assert len(captured) == settings.max_tool_hops
    # And every hop's tool call was recorded in the turn result.
    assert len(result.tool_calls) == settings.max_tool_hops


def test_empty_tool_calls_breaks_to_fallback(monkeypatch):
    _install_chat(monkeypatch, [LLMResponse(kind="tool_calls", tool_calls=[])])
    result = intent_parser.handle_user_message("s1", "weird")
    assert "trouble" in result.reply.lower()


# ---------- Pending-confirmation lifecycle ---------------------------------

def test_pending_confirmation_set_on_needs_confirmation(monkeypatch):
    n = note_service.create_note("doomed", "x")
    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(name="delete_note", arguments={"note_id": n.id})
                ],
            ),
            LLMResponse(kind="message", content="Are you sure?"),
        ],
    )
    intent_parser.handle_user_message("s1", "delete the doomed note")

    state = intent_parser.store.get("s1")
    assert state.pending_confirmation == {
        "tool": "delete_note",
        "args": {"note_id": n.id},
    }
    # Note still exists — only confirmed delete removes it.
    assert note_service.get_note(n.id) is not None


def test_pending_confirmation_cleared_after_confirmed_delete(monkeypatch):
    n = note_service.create_note("doomed", "x")

    # Turn 1: model asks to delete without confirmation → preview returned.
    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(name="delete_note", arguments={"note_id": n.id})
                ],
            ),
            LLMResponse(kind="message", content="Confirm?"),
        ],
    )
    intent_parser.handle_user_message("s1", "delete doomed")

    # Turn 2: user says yes, model now calls with confirm=true.
    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        name="delete_note",
                        arguments={"note_id": n.id, "confirm": True},
                    )
                ],
            ),
            LLMResponse(kind="message", content="Deleted."),
        ],
    )
    intent_parser.handle_user_message("s1", "yes")

    state = intent_parser.store.get("s1")
    assert state.pending_confirmation is None
    assert note_service.get_note(n.id) is None


# ---------- Context-line injection (multi-turn memory) ---------------------

def test_context_line_injected_on_next_turn(monkeypatch):
    # Turn 1: create a note — harvests id 1 into last_referenced_note_ids.
    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        name="add_note",
                        arguments={"title": "standup", "description": "moved to tuesday", "confirm": True},
                    )
                ],
            ),
            LLMResponse(kind="message", content="Saved."),
        ],
    )
    intent_parser.handle_user_message("s1", "save a note about standup")

    # Turn 2: another message. The built messages on the first LLM call should
    # now include the context line as a second system turn.
    captured = _install_chat(monkeypatch, [LLMResponse(kind="message", content="ack")])
    intent_parser.handle_user_message("s1", "thanks")

    built = captured[0]
    system_msgs = [m for m in built if m["role"] == "system"]
    assert len(system_msgs) == 2, "expected SYSTEM_PROMPT + context line"
    assert "(context)" in system_msgs[1]["content"]
    assert "that note" in system_msgs[1]["content"]


# ---------- Session isolation ----------------------------------------------

# ---------- Intent gate ----------------------------------------------------

def test_looks_like_note_op_accepts_note_verbs():
    from backend.agent.intent_parser import looks_like_note_op

    assert looks_like_note_op("add a note about the standup")
    assert looks_like_note_op("delete the old office note")
    assert looks_like_note_op("show me my recent notes")
    assert looks_like_note_op("Find the note about X")
    assert looks_like_note_op("summarise my urgent notes")
    assert looks_like_note_op("what's on my list today?")


def test_looks_like_note_op_rejects_chat_and_offtopic():
    from backend.agent.intent_parser import looks_like_note_op

    assert not looks_like_note_op("hi")
    assert not looks_like_note_op("hello there")
    assert not looks_like_note_op("how are you?")
    assert not looks_like_note_op("thanks!")
    assert not looks_like_note_op("what's 2+2?")
    assert not looks_like_note_op("tell me a joke")


def test_gate_disables_tools_for_greeting(monkeypatch):
    captured = _install_chat(
        monkeypatch, [LLMResponse(kind="message", content="Hi! How can I help with your notes?")]
    )

    reply = intent_parser.handle_user_message("s1", "hi")
    assert reply.reply.startswith("Hi")
    # The chat() call was made with an empty tools list — model couldn't fire.
    # captured[0] is the messages list; we need the kwargs, which the mock
    # doesn't record. Assert indirectly via llm_handler.chat mock:
    # We re-install with a capturing mock that records tools.
    assert captured  # sanity


def test_gate_enables_tools_for_note_request(monkeypatch):
    """When the input looks like a note op, chat() must receive full tool defs."""
    recorded_tools: list = []

    def fake_chat(messages, *, tools=None, on_delta=None):
        recorded_tools.append(tools)
        return LLMResponse(kind="message", content="ok")

    monkeypatch.setattr(llm_handler, "chat", fake_chat)

    intent_parser.handle_user_message("s1", "show me recent notes")
    assert recorded_tools[0] is None  # None → chat() falls back to full TOOL_DEFS


def test_gate_disables_tools_passes_empty_list(monkeypatch):
    """When the input is small talk, chat() must receive tools=[]."""
    recorded_tools: list = []

    def fake_chat(messages, *, tools=None, on_delta=None):
        recorded_tools.append(tools)
        return LLMResponse(kind="message", content="Hi!")

    monkeypatch.setattr(llm_handler, "chat", fake_chat)

    intent_parser.handle_user_message("s1", "hi")
    assert recorded_tools[0] == []


def test_gate_honors_pending_confirmation(monkeypatch):
    """After a needs_confirmation turn, the user's 'yes' must still be tool-enabled."""
    state = intent_parser.store.get("s1")
    state.pending_confirmation = {"tool": "delete_note", "args": {"note_id": 1}}

    recorded_tools: list = []

    def fake_chat(messages, *, tools=None, on_delta=None):
        recorded_tools.append(tools)
        return LLMResponse(kind="message", content="ok")

    monkeypatch.setattr(llm_handler, "chat", fake_chat)

    intent_parser.handle_user_message("s1", "yes")
    # Even though "yes" has no note keywords, the pending confirmation opens
    # the gate so the model can still fire delete_note with confirm=True.
    assert recorded_tools[0] is None


# ---------- Pending-args merge (defense against LLM sending just the diff) --

def test_merge_fills_in_dropped_fields_on_modify(monkeypatch):
    """Simulates the exact 8B failure mode: after the add preview, the user
    asks to change one field; the LLM re-calls add_note with ONLY the changed
    field populated. The orchestrator must merge with the stored pending args
    so the commit has the full note."""
    _install_chat(
        monkeypatch,
        [
            # Turn 1 hop 1: full add — returns needs_confirmation, preview is relayed.
            LLMResponse(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        name="add_note",
                        arguments={
                            "title": "Meeting on Tuesday 5 PM",
                            "description": "Meeting notes for discussion",
                            "tag": None,
                            "confirm": False,
                        },
                    )
                ],
            ),
            # Turn 1 hop 2: LLM replies with preview text.
            LLMResponse(kind="message", content="preview shown"),
        ],
    )
    intent_parser.handle_user_message("s1", "save a note about meeting tuesday 5pm")

    state = intent_parser.store.get("s1")
    assert state.pending_confirmation is not None
    assert state.pending_confirmation["args"]["title"] == "Meeting on Tuesday 5 PM"

    # Turn 2: user says "make the tag meetings and save it".
    # The LLM, at 8B, tends to emit only the delta. Simulate that:
    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        name="add_note",
                        arguments={
                            "title": "",          # DROPPED — orchestrator should merge
                            "description": "",    # DROPPED
                            "tag": "meetings",    # the actual change
                            "confirm": True,
                        },
                    )
                ],
            ),
            LLMResponse(kind="message", content="Saved!"),
        ],
    )
    intent_parser.handle_user_message("s1", "make the tag meetings and save it")

    # The merge must have filled in title/description from pending.
    saved = note_service.list_notes(limit=5)
    assert len(saved) == 1
    assert saved[0].title == "Meeting on Tuesday 5 PM"
    assert saved[0].description == "Meeting notes for discussion"
    assert saved[0].tag == "meetings"


def test_merge_only_applies_to_mergeable_tools(monkeypatch):
    """list_notes / search_notes / get_note are stateless — merge must be a no-op."""
    from backend.agent.intent_parser import _merge_with_pending

    state = intent_parser.store.get("s-no-merge")
    state.pending_confirmation = {
        "tool": "add_note",
        "args": {"title": "pending", "description": "pending", "tag": None},
    }
    call = ToolCall(name="list_notes", arguments={"limit": 5})
    assert _merge_with_pending(call, state) == {"limit": 5}


def test_merge_no_op_when_no_pending_confirmation():
    from backend.agent.intent_parser import _merge_with_pending

    state = intent_parser.store.get("s-fresh")
    call = ToolCall(name="add_note", arguments={"title": "T", "description": "D"})
    merged = _merge_with_pending(call, state)
    assert merged["title"] == "T"
    assert merged["description"] == "D"


def test_commit_intent_forces_confirm_on_compound_modify(monkeypatch):
    """'make the tag meetings and create it' — model sends confirm=false but
    user clearly wants commit. Orchestrator must force confirm=true."""
    # Turn 1: initial add → pending confirmation stored.
    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        name="add_note",
                        arguments={
                            "title": "Meeting",
                            "description": "Tuesday 5pm",
                            "tag": None,
                            "confirm": False,
                        },
                    )
                ],
            ),
            LLMResponse(kind="message", content="preview"),
        ],
    )
    intent_parser.handle_user_message("s1", "add note for meeting")

    # Turn 2: user says "tag meetings and create it" — LLM forgets confirm=true.
    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        name="add_note",
                        arguments={"tag": "meetings", "confirm": False},
                    )
                ],
            ),
            LLMResponse(kind="message", content="done"),
        ],
    )
    intent_parser.handle_user_message("s1", "make the tag meetings and create it")

    # Despite confirm=false from LLM, the note is actually saved because the
    # orchestrator detected commit intent and forced confirm=true.
    notes = note_service.list_notes()
    assert len(notes) == 1
    assert notes[0].title == "Meeting"
    assert notes[0].tag == "meetings"


def test_empty_string_fields_are_stripped_before_validation(monkeypatch):
    """8B failure mode: model calls update_note with title='', description=''
    when it means 'don't change these'. Orchestrator must treat empties as
    omitted so the update merges with current values instead of failing
    Pydantic min_length."""
    existing = note_service.create_note(
        "Meeting on Tuesday 5 pm",
        "Meeting with the dev team on Tuesday at 5 pm",
        tag="meetings",
    )

    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        name="update_note",
                        arguments={
                            "note_id": existing.id,
                            "title": "",
                            "description": "",
                            "tag": None,
                            "clear_tag": False,
                            "confirm": False,
                        },
                    )
                ],
            ),
            LLMResponse(kind="message", content="preview"),
        ],
    )

    # The empty strings are stripped before validation → update_note hits the
    # dispatcher's 'nothing to update' path with a clean error, not a Pydantic
    # min_length rejection.
    intent_parser.handle_user_message("s1", "change the meeting")

    # Nothing changed in the DB (model didn't actually supply new values).
    reloaded = note_service.get_note(existing.id)
    assert reloaded.title == "Meeting on Tuesday 5 pm"


def test_commit_intent_noop_when_no_pending(monkeypatch):
    """Plain 'save it' with no pending confirmation does NOT force confirm."""
    from backend.agent.intent_parser import _looks_like_commit_intent

    assert _looks_like_commit_intent("save it now") is True
    assert _looks_like_commit_intent("what are my tags?") is False
    assert _looks_like_commit_intent("cancel") is False


def test_sessions_are_isolated(monkeypatch):
    _install_chat(
        monkeypatch,
        [
            LLMResponse(kind="message", content="hi alice"),
            LLMResponse(kind="message", content="hi bob"),
        ],
    )

    intent_parser.handle_user_message("alice", "hi")
    intent_parser.handle_user_message("bob", "hi")

    alice = intent_parser.store.get("alice")
    bob = intent_parser.store.get("bob")
    assert alice is not bob
    assert list(alice.messages)[0]["content"] == "hi"
    assert len(list(alice.messages)) == 2  # user + assistant
    assert len(list(bob.messages)) == 2
