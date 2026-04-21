from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.agent import conversation_state, intent_parser, llm_handler
from backend.agent.llm_handler import LLMResponse, ToolCall
from backend.config import settings
from backend.db import sqlite as sqlite_mod
from backend.services import note_service


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
    yield


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

    reply = intent_parser.handle_user_message("s1", "hi")

    assert reply == "hello!"
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
                    ToolCall(name="add_note", arguments={"title": "t", "body": "b"})
                ],
            ),
            LLMResponse(kind="message", content="Done — created note."),
        ],
    )

    reply = intent_parser.handle_user_message("s1", "save a note")

    assert "Done" in reply
    recent = note_service.list_recent(limit=5)
    assert len(recent) == 1 and recent[0].title == "t"


def test_message_history_shape_after_tool_call(monkeypatch):
    _install_chat(
        monkeypatch,
        [
            LLMResponse(
                kind="tool_calls",
                tool_calls=[ToolCall(name="list_recent", arguments={"limit": 3})],
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
    assert tool_msg["name"] == "list_recent"
    assert '"ok": true' in tool_msg["content"] or '"ok":true' in tool_msg["content"]


# ---------- MAX_TOOL_HOPS --------------------------------------------------

def test_max_tool_hops_triggers_fallback(monkeypatch):
    looping = LLMResponse(
        kind="tool_calls",
        tool_calls=[ToolCall(name="list_recent", arguments={"limit": 1})],
    )
    # Supply more than enough to guarantee we hit the cap.
    captured = _install_chat(monkeypatch, [looping] * 20)

    reply = intent_parser.handle_user_message("s1", "loop forever")

    assert "trouble" in reply.lower()
    # Exactly max_tool_hops LLM calls were made.
    assert len(captured) == settings.max_tool_hops


def test_empty_tool_calls_breaks_to_fallback(monkeypatch):
    _install_chat(monkeypatch, [LLMResponse(kind="tool_calls", tool_calls=[])])
    reply = intent_parser.handle_user_message("s1", "weird")
    assert "trouble" in reply.lower()


# ---------- Pending-confirmation lifecycle ---------------------------------

def test_pending_confirmation_set_on_needs_confirmation(monkeypatch):
    n = note_service.create_note("doomed", "x", [])
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
    n = note_service.create_note("doomed", "x", [])

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
                        arguments={"title": "standup", "body": "moved to tuesday"},
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
