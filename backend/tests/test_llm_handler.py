from __future__ import annotations

from typing import Any

import pytest

from backend.agent import llm_handler
from backend.agent.llm_handler import LLMResponse, ToolCall, _try_parse_toolcall_from_text


class _FakeClient:
    """Minimal stand-in for ollama.Client used only by these tests."""

    def __init__(self, canned: Any):
        self.canned = canned
        self.last_call: dict | None = None

    def chat(self, **kwargs):
        self.last_call = kwargs
        return self.canned


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient(canned={"message": {"role": "assistant", "content": ""}})
    monkeypatch.setattr(llm_handler, "_client", client)
    return client


# ---------- chat() happy paths ---------------------------------------------

def test_chat_returns_tool_calls_when_message_has_tool_calls(fake_client):
    fake_client.canned = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "list_notes",
                        "arguments": {"limit": 3},
                    }
                }
            ],
        }
    }

    resp = llm_handler.chat([{"role": "user", "content": "show recent"}])

    assert isinstance(resp, LLMResponse)
    assert resp.kind == "tool_calls"
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert call.name == "list_notes"
    assert call.arguments == {"limit": 3}


def test_chat_returns_plain_message_when_no_tool_calls(fake_client):
    fake_client.canned = {
        "message": {
            "role": "assistant",
            "content": "Hi there — what can I help with?",
        }
    }
    resp = llm_handler.chat([{"role": "user", "content": "hi"}])
    assert resp.kind == "message"
    assert resp.content == "Hi there — what can I help with?"
    assert resp.tool_calls == []


def test_chat_parses_stringified_arguments(fake_client):
    """Some Ollama builds hand back arguments as JSON-encoded strings."""
    fake_client.canned = {
        "message": {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "get_note",
                        "arguments": '{"note_id": 42}',
                    }
                }
            ],
        }
    }
    resp = llm_handler.chat([])
    assert resp.kind == "tool_calls"
    assert resp.tool_calls[0].arguments == {"note_id": 42}


def test_chat_handles_pydantic_model_response(fake_client):
    """Newer ollama SDK returns a pydantic model, not a raw dict."""

    class _FakeModel:
        def model_dump(self):
            return {
                "message": {
                    "role": "assistant",
                    "content": "ok",
                }
            }

    fake_client.canned = _FakeModel()
    resp = llm_handler.chat([])
    assert resp.kind == "message"
    assert resp.content == "ok"


def test_chat_passes_tools_model_and_temperature(fake_client):
    fake_client.canned = {"message": {"content": "hi"}}
    llm_handler.chat([{"role": "user", "content": "x"}])

    kwargs = fake_client.last_call
    assert kwargs is not None
    assert kwargs["model"] == llm_handler.settings.ollama_model
    assert kwargs["options"] == {"temperature": 0.2}
    # Tools default to TOOL_DEFS — sanity-check a known name is present.
    names = {t["function"]["name"] for t in kwargs["tools"]}
    assert {"add_note", "delete_note"}.issubset(names)


def test_chat_allows_tools_override(fake_client):
    fake_client.canned = {"message": {"content": "hi"}}
    llm_handler.chat([], tools=[])
    assert fake_client.last_call["tools"] == []


# ---------- JSON-repair fallback -------------------------------------------

def test_chat_repairs_flat_json_toolcall_from_content(fake_client):
    fake_client.canned = {
        "message": {
            "role": "assistant",
            "content": '{"name": "list_notes", "arguments": {"limit": 2}}',
        }
    }
    resp = llm_handler.chat([])
    assert resp.kind == "tool_calls"
    assert resp.tool_calls[0].name == "list_notes"
    assert resp.tool_calls[0].arguments == {"limit": 2}


def test_chat_repairs_wrapped_json_toolcall_from_content(fake_client):
    fake_client.canned = {
        "message": {
            "role": "assistant",
            "content": 'Here you go: {"function": {"name": "get_note", "arguments": {"note_id": 7}}} done.',
        }
    }
    resp = llm_handler.chat([])
    assert resp.kind == "tool_calls"
    assert resp.tool_calls[0].name == "get_note"
    assert resp.tool_calls[0].arguments == {"note_id": 7}


def test_chat_falls_back_to_message_when_content_is_malformed_json(fake_client):
    fake_client.canned = {
        "message": {
            "role": "assistant",
            "content": "here's some text {not actually: json} more text",
        }
    }
    resp = llm_handler.chat([])
    assert resp.kind == "message"
    assert "some text" in resp.content


def test_chat_ignores_json_with_unknown_tool_name(fake_client):
    fake_client.canned = {
        "message": {
            "role": "assistant",
            "content": '{"name": "do_the_thing", "arguments": {}}',
        }
    }
    resp = llm_handler.chat([])
    assert resp.kind == "message"


# ---------- _try_parse_toolcall_from_text direct unit tests ----------------

def test_parser_flat_shape():
    result = _try_parse_toolcall_from_text('{"name": "add_note", "arguments": {"title": "t", "description": "b"}}')
    assert isinstance(result, ToolCall)
    assert result.name == "add_note"
    assert result.arguments == {"title": "t", "description": "b"}


def test_parser_wrapped_shape():
    result = _try_parse_toolcall_from_text('{"function": {"name": "list_notes", "arguments": {"limit": 5}}}')
    assert isinstance(result, ToolCall)
    assert result.name == "list_notes"


def test_parser_handles_stringified_arguments_in_nested_shape():
    result = _try_parse_toolcall_from_text(
        '{"function": {"name": "get_note", "arguments": "{\\"note_id\\": 1}"}}'
    )
    assert isinstance(result, ToolCall)
    assert result.arguments == {"note_id": 1}


def test_parser_none_when_empty():
    assert _try_parse_toolcall_from_text("") is None
    assert _try_parse_toolcall_from_text("   ") is None


def test_parser_none_when_no_json_block():
    assert _try_parse_toolcall_from_text("just some plain text") is None


def test_parser_none_when_invalid_json():
    assert _try_parse_toolcall_from_text("{this is not: json}") is None


def test_parser_none_for_unknown_tool_name():
    assert _try_parse_toolcall_from_text('{"name": "bogus_tool", "arguments": {}}') is None


def test_parser_none_for_non_object_json():
    assert _try_parse_toolcall_from_text('["list", "not", "object"]') is None
