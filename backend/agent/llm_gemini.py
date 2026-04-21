from __future__ import annotations

import copy
import json
import logging
from typing import Any, Callable

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from backend.agent.llm_types import LLMResponse, ToolCall
from backend.config import settings
from backend.tools.schemas import TOOL_DEFS

logger = logging.getLogger(__name__)


class GeminiError(RuntimeError):
    """Clean, user-facing error for Gemini failures. Replaces the giant
    JSON-blob stack traces that leak from the raw SDK."""


def _cleanup_gemini_error(model: str, exc: BaseException) -> GeminiError:
    """Map a raw Gemini SDK error to a short message suitable for the UI."""
    if isinstance(exc, genai_errors.ClientError):
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if status == 429:
            return GeminiError(
                f"Gemini rate limit hit for '{model}'. "
                "This model may require a paid API tier — try Gemini 2.5 Flash or Ollama."
            )
        if status == 403:
            return GeminiError(
                f"Gemini refused the request (403) — check that the API key has "
                f"access to '{model}'."
            )
        if status == 400:
            # Surface the first meaningful line of the error to help debug.
            msg = str(exc).split("\n", 1)[0]
            return GeminiError(f"Gemini rejected the request: {msg}")
    if isinstance(exc, genai_errors.ServerError):
        return GeminiError(
            f"Gemini is having problems ({exc.__class__.__name__}). Try again in a moment."
        )
    return GeminiError(f"Gemini error ({exc.__class__.__name__}): {str(exc)[:200]}")


# Lazy singleton — tests can monkeypatch `_client` without invoking the SDK.
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Configure it in .env to use Gemini models."
            )
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


# --- Message format translation --------------------------------------------
#
# Our canonical message shape (same one Ollama uses):
#   {"role": "system",    "content": "..."}
#   {"role": "user",      "content": "..."}
#   {"role": "assistant", "content": "..."}
#   {"role": "assistant", "tool_calls": [{"function": {"name": ..., "arguments": {...}}}]}
#   {"role": "tool",      "name": ..., "content": "<JSON of ToolResult>"}
#
# Gemini's shape:
#   system_instruction: "..."  (flat string or Content)
#   contents: [
#     {"role": "user"  | "model", "parts": [{"text": "..."}]},
#     {"role": "model",           "parts": [{"function_call": {"name":..., "args":{}}}]},
#     {"role": "user",            "parts": [{"function_response": {"name":..., "response":{}}}]}
#   ]

def _translate_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    system_bits: list[str] = []
    contents: list[dict] = []

    for m in messages:
        role = m.get("role")
        if role == "system":
            text = (m.get("content") or "").strip()
            if text:
                system_bits.append(text)

        elif role == "user":
            contents.append(
                {"role": "user", "parts": [{"text": m.get("content") or ""}]}
            )

        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                parts: list[dict] = []
                for tc in tool_calls:
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    parts.append(
                        {
                            "function_call": {
                                "name": fn.get("name", ""),
                                "args": fn.get("arguments") or {},
                            }
                        }
                    )
                contents.append({"role": "model", "parts": parts})
            else:
                contents.append(
                    {"role": "model", "parts": [{"text": m.get("content") or ""}]}
                )

        elif role == "tool":
            raw_content = m.get("content") or "{}"
            try:
                response_obj = json.loads(raw_content)
            except json.JSONDecodeError:
                response_obj = {"raw": raw_content}
            if not isinstance(response_obj, dict):
                response_obj = {"result": response_obj}
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": m.get("name", ""),
                                "response": response_obj,
                            }
                        }
                    ],
                }
            )

    system_instruction = "\n\n".join(system_bits) if system_bits else None
    return system_instruction, contents


# --- Tool schema translation -----------------------------------------------
#
# Our TOOL_DEFS format (OpenAI-style):
#   {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
#
# Gemini wants:
#   types.Tool(function_declarations=[types.FunctionDeclaration(name=..., description=..., parameters=...)])
#
# Gemini's parameters are JSON Schema but with restrictions: no $ref, no $defs,
# and nullable fields need `nullable: true` instead of `anyOf: [X, null]`.

# Schema metadata keys that Gemini doesn't accept at the schema-node level.
# NOTE: we do NOT drop `title` globally — it's a legal Pydantic metadata key at
# the schema level but is ALSO a valid property name (our AddNoteArgs has a
# `title` property). We only strip it from schema nodes themselves, never
# from inside a `properties` dict.
_UNSUPPORTED_AT_NODE = {"$schema"}


def _normalize_schema_for_gemini(schema: dict) -> dict:
    """Inline $refs, collapse anyOf[X, null] → {X, nullable: true}, drop
    schema keys Gemini rejects. Recursive. Non-destructive on the input."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {}) or {}

    def walk_schema(node: Any) -> Any:
        """Walk a JSON-schema NODE. Metadata keys like $schema / $defs /
        title are interpreted as schema metadata here and stripped/inlined."""
        if isinstance(node, list):
            return [walk_schema(x) for x in node]
        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            ref = node["$ref"]
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/") :]
                resolved = defs.get(name, {})
                merged = dict(resolved)
                for k, v in node.items():
                    if k != "$ref":
                        merged.setdefault(k, v)
                return walk_schema(merged)
            return node

        if "anyOf" in node and isinstance(node["anyOf"], list):
            options = node["anyOf"]
            non_null = [o for o in options if o.get("type") != "null"]
            has_null = any(o.get("type") == "null" for o in options)
            if has_null and len(non_null) == 1:
                merged = dict(non_null[0])
                merged["nullable"] = True
                for k, v in node.items():
                    if k != "anyOf":
                        merged.setdefault(k, v)
                return walk_schema(merged)

        out: dict = {}
        for k, v in node.items():
            if k in _UNSUPPORTED_AT_NODE or k == "$defs":
                continue
            # `title` as a schema-metadata key (value is a string) is cosmetic —
            # drop it. As a property NAME inside `properties`, it's meaningful
            # — handled below by not recursing into properties with walk_schema.
            if k == "title" and isinstance(v, str):
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {
                    # Do NOT strip/rewrite property names; they're user data.
                    prop_name: walk_schema(prop_schema)
                    for prop_name, prop_schema in v.items()
                }
            else:
                out[k] = walk_schema(v)
        return out

    return walk_schema(schema)


def _translate_tools(tool_defs: list[dict]) -> list[genai_types.Tool]:
    declarations: list[genai_types.FunctionDeclaration] = []
    for entry in tool_defs:
        fn = entry.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        params = _normalize_schema_for_gemini(fn.get("parameters") or {})
        declarations.append(
            genai_types.FunctionDeclaration(
                name=name,
                description=fn.get("description") or "",
                parameters=params,
            )
        )
    if not declarations:
        return []
    return [genai_types.Tool(function_declarations=declarations)]


# --- Response normalization ------------------------------------------------

def _safety_block_none() -> list[genai_types.SafetySetting]:
    """The note app has zero safety concerns — turn off Gemini's content
    filters so benign note text (e.g. "delete everything") isn't blocked."""
    categories = [
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    ]
    return [
        genai_types.SafetySetting(category=cat, threshold="BLOCK_NONE")
        for cat in categories
    ]


def _extract_tool_calls_from_parts(parts: list) -> list[ToolCall]:
    out: list[ToolCall] = []
    for part in parts or []:
        fc = getattr(part, "function_call", None)
        if fc and getattr(fc, "name", None):
            args = getattr(fc, "args", None) or {}
            # Gemini may return a MapComposite or dict-like — normalize to dict.
            if not isinstance(args, dict):
                try:
                    args = dict(args)
                except Exception:
                    args = {}
            out.append(ToolCall(name=fc.name, arguments=args))
    return out


def _extract_text_from_parts(parts: list) -> str:
    buf: list[str] = []
    for part in parts or []:
        text = getattr(part, "text", None)
        if text:
            buf.append(text)
    return "".join(buf)


# --- Public entry point ----------------------------------------------------

def chat(
    messages: list[dict],
    *,
    model: str,
    tools: list[dict] | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> LLMResponse:
    """Send `messages` to Gemini and return a normalized LLMResponse.

    `model` is the concrete Gemini model id (e.g. "gemini-2.5-pro"). Streaming
    is enabled when `on_delta` is provided.
    """
    client = _get_client()
    system_instruction, contents = _translate_messages(messages)
    gemini_tools = _translate_tools(tools if tools is not None else TOOL_DEFS)

    config = genai_types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=gemini_tools or None,
        temperature=0.2,
        safety_settings=_safety_block_none(),
    )

    if on_delta is not None:
        try:
            return _chat_streaming(client, model, contents, config, on_delta)
        except genai_errors.APIError as e:
            raise _cleanup_gemini_error(model, e) from e

    try:
        resp = client.models.generate_content(
            model=model, contents=contents, config=config
        )
    except genai_errors.APIError as e:
        raise _cleanup_gemini_error(model, e) from e
    return _normalize_response(resp)


def _chat_streaming(
    client: genai.Client,
    model: str,
    contents: list[dict],
    config: genai_types.GenerateContentConfig,
    on_delta: Callable[[str], None],
) -> LLMResponse:
    full_text_parts: list[str] = []
    collected_tool_calls: list[ToolCall] = []

    stream = client.models.generate_content_stream(
        model=model, contents=contents, config=config
    )
    for chunk in stream:
        # Stream text deltas immediately.
        text = getattr(chunk, "text", None)
        if text:
            full_text_parts.append(text)
            on_delta(text)

        # Collect function_call parts as they arrive (usually last chunk).
        candidates = getattr(chunk, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            if content is None:
                continue
            parts = getattr(content, "parts", None) or []
            collected_tool_calls.extend(_extract_tool_calls_from_parts(parts))

    if collected_tool_calls:
        return LLMResponse(kind="tool_calls", tool_calls=collected_tool_calls)
    return LLMResponse(kind="message", content="".join(full_text_parts))


def _normalize_response(resp: Any) -> LLMResponse:
    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        return LLMResponse(kind="message", content="")

    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or [] if content else []

    tool_calls = _extract_tool_calls_from_parts(parts)
    if tool_calls:
        return LLMResponse(kind="tool_calls", tool_calls=tool_calls)

    text = _extract_text_from_parts(parts)
    return LLMResponse(kind="message", content=text)
