from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from backend.agent import intent_parser
from backend.agent.intent_parser import TurnResult, TurnToolCall
from backend.db import sqlite as sqlite_mod
from backend.main import app
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
    """Startup lifespan calls init_db — point it at a tmp file per test."""
    db_file = tmp_path / "notes.db"
    monkeypatch.setattr(sqlite_mod, "settings", _FakeSettings(db_path=str(db_file)))
    yield str(db_file)


@pytest.fixture
def client() -> TestClient:
    # TestClient triggers lifespan on enter, which runs init_db() against the
    # monkeypatched settings from tmp_db.
    with TestClient(app) as c:
        yield c


# ---------- /chat ----------------------------------------------------------

def test_chat_returns_reply_shape(client, monkeypatch):
    def fake_handle(session_id, message):
        return TurnResult(reply=f"echo: {message}", tool_calls=[])

    monkeypatch.setattr(intent_parser, "handle_user_message", fake_handle)

    r = client.post("/chat", json={"session_id": "s1", "message": "hi"})

    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s1"
    assert body["reply"] == "echo: hi"
    assert body["tool_calls"] == []


def test_chat_serializes_tool_calls(client, monkeypatch):
    def fake_handle(session_id, message):
        tool_result = ToolResult(
            ok=True,
            message="Created note #17.",
            data={"id": 17, "title": "standup"},
        )
        return TurnResult(
            reply="Saved.",
            tool_calls=[
                TurnToolCall(
                    id="tc-abcd1234",
                    name="add_note",
                    arguments={"title": "standup", "body": "moved"},
                    result=tool_result,
                )
            ],
        )

    monkeypatch.setattr(intent_parser, "handle_user_message", fake_handle)

    r = client.post(
        "/chat",
        json={"session_id": "s1", "message": "save a note"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "Saved."
    assert len(body["tool_calls"]) == 1

    tc = body["tool_calls"][0]
    assert tc["id"] == "tc-abcd1234"
    assert tc["name"] == "add_note"
    assert tc["arguments"] == {"title": "standup", "body": "moved"}
    # ToolResult serialized verbatim — the frontend reads `.ok`, `.message`,
    # `.needs_confirmation`, `.error_code` directly off this nested dict.
    assert tc["result"]["ok"] is True
    assert tc["result"]["message"] == "Created note #17."
    assert tc["result"]["data"] == {"id": 17, "title": "standup"}


def test_chat_preserves_needs_confirmation(client, monkeypatch):
    def fake_handle(session_id, message):
        tool_result = ToolResult(
            ok=False,
            needs_confirmation=True,
            error_code="needs_confirmation",
            message="Confirm deletion of #8",
            data={"preview": {"id": 8, "title": "old"}},
        )
        return TurnResult(
            reply="Delete note 8?",
            tool_calls=[
                TurnToolCall(
                    id="tc-1",
                    name="delete_note",
                    arguments={"note_id": 8},
                    result=tool_result,
                )
            ],
        )

    monkeypatch.setattr(intent_parser, "handle_user_message", fake_handle)

    r = client.post("/chat", json={"session_id": "s1", "message": "delete old note"})
    body = r.json()
    assert body["tool_calls"][0]["result"]["needs_confirmation"] is True
    assert body["tool_calls"][0]["result"]["error_code"] == "needs_confirmation"


def test_chat_rejects_empty_message(client):
    r = client.post("/chat", json={"session_id": "s1", "message": ""})
    assert r.status_code == 422


def test_chat_rejects_missing_session_id(client):
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 422


# ---------- CORS -----------------------------------------------------------

def test_cors_preflight_from_nextjs_origin(client):
    r = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_allows_port_3001_fallback(client):
    r = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3001",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3001"


# ---------- /chat/stream (SSE) --------------------------------------------

def _parse_sse(payload: str) -> list[tuple[str, dict]]:
    """Split an SSE body into (event_type, data_dict) tuples."""
    import json as _json

    out: list[tuple[str, dict]] = []
    for frame in payload.strip().split("\n\n"):
        if not frame.strip():
            continue
        event_type: str | None = None
        data_str: str | None = None
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event_type = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data_str = line[len("data: ") :]
        assert event_type is not None and data_str is not None, f"bad frame: {frame!r}"
        out.append((event_type, _json.loads(data_str)))
    return out


def test_stream_emits_full_happy_sequence(client, monkeypatch):
    def fake_handle(session_id, message, emit=None):
        assert emit is not None
        emit("user_echo", {"message": message})
        emit(
            "tool_call",
            {
                "id": "tc-1",
                "name": "add_note",
                "arguments": {"title": "t", "body": "b"},
                "status": "running",
            },
        )
        emit(
            "tool_result",
            {
                "id": "tc-1",
                "status": "ok",
                "message": "Created.",
                "error_code": None,
                "data": {"id": 17},
                "needs_confirmation": False,
                "candidates": None,
            },
        )
        emit("assistant", {"content": "Saved!"})
        emit("done", {})
        return intent_parser.TurnResult(reply="Saved!", tool_calls=[])

    monkeypatch.setattr(intent_parser, "handle_user_message", fake_handle)

    r = client.post(
        "/chat/stream",
        json={"session_id": "s1", "message": "add t/b"},
    )

    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = _parse_sse(r.text)
    types = [t for t, _ in events]
    assert types == [
        "user_echo",
        "tool_call",
        "tool_result",
        "assistant",
        "done",
    ]
    # Values survive the round-trip.
    assert events[0][1]["message"] == "add t/b"
    assert events[1][1]["name"] == "add_note"
    assert events[2][1]["status"] == "ok"
    assert events[3][1]["content"] == "Saved!"


def test_stream_emits_error_event_on_orchestrator_exception(client, monkeypatch):
    def fake_handle(session_id, message, emit=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(intent_parser, "handle_user_message", fake_handle)

    r = client.post(
        "/chat/stream",
        json={"session_id": "s1", "message": "x"},
    )
    events = _parse_sse(r.text)
    assert any(t == "error" for t, _ in events)
    err = next(d for t, d in events if t == "error")
    assert "boom" in err["message"]


def test_stream_rejects_invalid_body(client):
    r = client.post("/chat/stream", json={"session_id": "", "message": "x"})
    assert r.status_code == 422


# ---------- / (root pointer) ----------------------------------------------

def test_root_returns_pointer_to_ui_and_docs(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "Note Agent API"
    assert body["ui"] == "http://localhost:3000"
    assert "/chat" in body["endpoints"]


# ---------- /health --------------------------------------------------------

def test_health_endpoint_is_reachable(client, monkeypatch):
    # Don't let health poke the real ollama daemon in tests.
    class _FakeOllama:
        def list(self):
            return {"models": [{"name": "llama3.1:latest"}]}

    monkeypatch.setattr("backend.main.ollama", _FakeOllama())
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["model"] == "llama3.1"
