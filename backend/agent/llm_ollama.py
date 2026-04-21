from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from ollama import Client

from backend.agent.llm_types import LLMResponse, ToolCall
from backend.config import settings
from backend.tools.schemas import TOOL_DEFS, TOOL_NAMES

logger = logging.getLogger(__name__)


# --- Client plumbing --------------------------------------------------------
#
# Lazily initialised so unit tests can monkeypatch `_client` with a fake
# without the `Client(...)` constructor ever running (which would otherwise
# try to resolve the Ollama host at import time).

_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(host=settings.ollama_host)
    return _client


def _as_dict(resp: Any) -> dict:
    if hasattr(resp, "model_dump"):
        return resp.model_dump()
    if isinstance(resp, dict):
        return resp
    return dict(resp)


def _coerce_arguments(raw: Any) -> dict:
    """Normalize whatever the SDK handed us into a dict.

    Some Ollama versions return tool-call `arguments` as a JSON-encoded string
    instead of a parsed object. Down-stream code expects a dict, so we parse
    here once and only once.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Tool-call arguments were a non-JSON string: %r", raw)
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# --- Public entry point -----------------------------------------------------

def chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> LLMResponse:
    """Send `messages` to Ollama and return a normalized `LLMResponse`.

    Temperature is fixed at 0.2 — low enough to prefer tool calls over
    creative prose, but not zero so the model doesn't loop on refusals.
    """
    client = _get_client()
    if on_delta is not None:
        return _chat_streaming(client, messages, tools, on_delta)

    resp = client.chat(
        model=settings.ollama_model,
        messages=messages,
        tools=tools if tools is not None else TOOL_DEFS,
        options={"temperature": 0.2},
    )
    data = _as_dict(resp)
    return _normalize_response(data)


def _chat_streaming(
    client: Client,
    messages: list[dict],
    tools: list[dict] | None,
    on_delta: Callable[[str], None],
) -> LLMResponse:
    stream = client.chat(
        model=settings.ollama_model,
        messages=messages,
        tools=tools if tools is not None else TOOL_DEFS,
        options={"temperature": 0.2},
        stream=True,
    )

    full_content = ""
    accumulated_tool_calls: list[dict] = []
    last_chunk: dict | None = None

    for chunk in stream:
        data = _as_dict(chunk)
        last_chunk = data
        msg = data.get("message") or {}

        content_delta = msg.get("content") or ""
        if content_delta:
            full_content += content_delta
            on_delta(content_delta)

        raw_tool_calls = msg.get("tool_calls") or []
        if raw_tool_calls:
            accumulated_tool_calls.extend(raw_tool_calls)

    synthesized: dict = {
        "message": {
            "content": full_content,
            "tool_calls": accumulated_tool_calls,
        }
    }
    if last_chunk is not None:
        synthesized.update({k: v for k, v in last_chunk.items() if k != "message"})
    return _normalize_response(synthesized)


def _normalize_response(data: dict) -> LLMResponse:
    msg = data.get("message") or {}

    raw_tool_calls = msg.get("tool_calls") or []
    if raw_tool_calls:
        calls: list[ToolCall] = []
        for tc in raw_tool_calls:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "")
            arguments = _coerce_arguments(fn.get("arguments"))
            calls.append(ToolCall(name=name, arguments=arguments))
        return LLMResponse(kind="tool_calls", tool_calls=calls, raw=data)

    text = msg.get("content") or ""
    maybe = _try_parse_toolcall_from_text(text)
    if maybe is not None:
        logger.info("Recovered a tool call from assistant text: %s", maybe.name)
        return LLMResponse(kind="tool_calls", tool_calls=[maybe], raw=data)

    return LLMResponse(kind="message", content=text, raw=data)


# --- JSON-repair fallback ---------------------------------------------------
#
# Smaller llama variants occasionally emit a tool call as JSON inside `content`
# instead of populating `tool_calls`. Rather than let that reply dead-end,
# we salvage it here. Ollama-specific — Gemini doesn't need this.

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _try_parse_toolcall_from_text(text: str) -> ToolCall | None:
    if not text or not text.strip():
        return None

    match = _JSON_BLOCK_RE.search(text)
    if match is None:
        return None

    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    if not isinstance(obj, dict):
        return None

    fn = obj.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        args = _coerce_arguments(fn.get("arguments"))
        if isinstance(name, str) and name in TOOL_NAMES:
            return ToolCall(name=name, arguments=args)

    name = obj.get("name")
    if isinstance(name, str) and name in TOOL_NAMES:
        args = _coerce_arguments(obj.get("arguments"))
        return ToolCall(name=name, arguments=args)

    return None
