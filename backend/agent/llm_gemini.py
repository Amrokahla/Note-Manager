from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from typing import Any, Callable

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from backend.agent.llm_types import LLMResponse, ToolCall
from backend.config import settings
from backend.tools.schemas import TOOL_DEFS

logger = logging.getLogger(__name__)


_caches: dict[str, str] = {}
_cache_lock = threading.Lock()


def _cache_key(model: str, sys_instr: str | None, tools_input: list[dict] | None) -> str:
    """Hash the inputs that produced the request — translation is deterministic."""
    if not sys_instr or not tools_input:
        return ""
    canonical = f"{model}|{sys_instr}|{json.dumps(tools_input, sort_keys=True, default=str)}"
    return hashlib.sha256(canonical.encode()).hexdigest()


def _get_or_create_cache(
    client: genai.Client,
    model: str,
    sys_instr: str | None,
    tools_input: list[dict] | None,
    gemini_tools: list[genai_types.Tool],
) -> str | None:
    """Return a cache name for this (model, prompt, tools) triple, or None to skip caching."""
    if not settings.gemini_cache_enabled:
        return None
    key = _cache_key(model, sys_instr, tools_input)
    if not key:
        return None
    with _cache_lock:
        if key in _caches:
            return _caches[key]
        try:
            cache = client.caches.create(
                model=model,
                config=genai_types.CreateCachedContentConfig(
                    system_instruction=sys_instr,
                    tools=gemini_tools,
                    ttl=f"{settings.gemini_cache_ttl_seconds}s",
                    display_name=f"note-agent:{key[:12]}",
                ),
            )
        except genai_errors.APIError as e:
            logger.warning(
                "Gemini cache create failed (%s) — proceeding without cache", e
            )
            return None
        _caches[key] = cache.name
        logger.info(
            "Gemini cache created name=%s model=%s ttl=%ds",
            cache.name, model, settings.gemini_cache_ttl_seconds,
        )
        return cache.name


def _evict_cache(model: str, sys_instr: str | None, tools_input: list[dict] | None) -> None:
    key = _cache_key(model, sys_instr, tools_input)
    if not key:
        return
    with _cache_lock:
        _caches.pop(key, None)


def _is_cache_error(exc: BaseException) -> bool:
    return "cache" in str(exc).lower()


class GeminiError(RuntimeError):
    """User-facing error for Gemini failures; replaces raw SDK stack traces."""


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
            msg = str(exc).split("\n", 1)[0]
            return GeminiError(f"Gemini rejected the request: {msg}")
    if isinstance(exc, genai_errors.ServerError):
        return GeminiError(
            f"Gemini is having problems ({exc.__class__.__name__}). Try again in a moment."
        )
    return GeminiError(f"Gemini error ({exc.__class__.__name__}): {str(exc)[:200]}")


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
    return system_instruction, _repair_function_pairs(contents)


def _repair_function_pairs(contents: list[dict]) -> list[dict]:
    """Drop orphan function_call / function_response turns so Gemini's pairing rule holds."""
    def _is_fn_call_turn(c: dict) -> bool:
        return c.get("role") == "model" and any(
            isinstance(p, dict) and "function_call" in p for p in c.get("parts") or []
        )

    def _is_fn_response_turn(c: dict) -> bool:
        return c.get("role") == "user" and any(
            isinstance(p, dict) and "function_response" in p for p in c.get("parts") or []
        )

    out: list[dict] = []
    i = 0
    while i < len(contents):
        curr = contents[i]
        if _is_fn_call_turn(curr):
            nxt = contents[i + 1] if i + 1 < len(contents) else None
            if nxt is not None and _is_fn_response_turn(nxt):
                out.append(curr)
                out.append(nxt)
                i += 2
                continue
            logger.warning(
                "Dropping orphan function_call turn (no function_response follows): %r",
                curr.get("parts"),
            )
            i += 1
            continue
        if _is_fn_response_turn(curr):
            logger.warning(
                "Dropping orphan function_response turn (no preceding function_call): %r",
                curr.get("parts"),
            )
            i += 1
            continue
        out.append(curr)
        i += 1
    return out


_UNSUPPORTED_AT_NODE = {"$schema"}


def _normalize_schema_for_gemini(schema: dict) -> dict:
    """Inline $refs, collapse anyOf[X, null] to nullable, drop keys Gemini rejects."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {}) or {}

    def walk_schema(node: Any) -> Any:
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
            if k == "title" and isinstance(v, str):
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {
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


def _safety_block_none() -> list[genai_types.SafetySetting]:
    """Disable Gemini safety filters; note text has no sensitive content."""
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


def _coerce_function_call_args(fc: Any) -> dict:
    """Convert google-genai's FunctionCall.args to a plain dict; never raises."""
    args = getattr(fc, "args", None) or {}
    if isinstance(args, dict):
        return args
    try:
        return dict(args)
    except Exception:
        return {}


def _extract_tool_calls_from_parts(parts: list) -> list[ToolCall]:
    out: list[ToolCall] = []
    for part in parts or []:
        fc = getattr(part, "function_call", None)
        if fc and getattr(fc, "name", None):
            out.append(ToolCall(name=fc.name, arguments=_coerce_function_call_args(fc)))
    return out


def _extract_text_from_parts(parts: list) -> str:
    buf: list[str] = []
    for part in parts or []:
        text = getattr(part, "text", None)
        if text:
            buf.append(text)
    return "".join(buf)


def _log_usage(model: str, usage: Any) -> None:
    """Log Gemini token usage so we can verify implicit prompt caching is hitting."""
    if usage is None:
        return
    prompt = getattr(usage, "prompt_token_count", None) or 0
    cached = getattr(usage, "cached_content_token_count", None) or 0
    output = getattr(usage, "candidates_token_count", None) or 0
    total = getattr(usage, "total_token_count", None) or 0
    pct = (100.0 * cached / prompt) if prompt else 0.0
    logger.info(
        "gemini usage model=%s prompt=%d cached=%d (%.0f%%) output=%d total=%d",
        model, prompt, cached, pct, output, total,
    )


def chat(
    messages: list[dict],
    *,
    model: str,
    tools: list[dict] | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> LLMResponse:
    """Send `messages` to Gemini and return a normalized LLMResponse; streams when `on_delta` is set."""
    client = _get_client()
    system_instruction, contents = _translate_messages(messages)
    tools_input = tools if tools is not None else TOOL_DEFS
    gemini_tools = _translate_tools(tools_input)

    cache_name = _get_or_create_cache(
        client, model, system_instruction, tools_input, gemini_tools
    )

    try:
        return _do_chat(
            client, model, contents, system_instruction, gemini_tools, cache_name, on_delta
        )
    except genai_errors.APIError as e:
        if cache_name and _is_cache_error(e):
            logger.warning(
                "Gemini cache %s rejected (%s) — evicting and retrying without cache",
                cache_name, e,
            )
            _evict_cache(model, system_instruction, tools_input)
            try:
                return _do_chat(
                    client, model, contents, system_instruction, gemini_tools, None, on_delta
                )
            except genai_errors.APIError as e2:
                raise _cleanup_gemini_error(model, e2) from e2
        raise _cleanup_gemini_error(model, e) from e


def _do_chat(
    client: genai.Client,
    model: str,
    contents: list[dict],
    sys_instr: str | None,
    gemini_tools: list[genai_types.Tool],
    cache_name: str | None,
    on_delta: Callable[[str], None] | None,
) -> LLMResponse:
    thinking_config = (
        genai_types.ThinkingConfig(thinking_budget=0)
        if "flash" in model.lower()
        else None
    )

    if cache_name:
        config = genai_types.GenerateContentConfig(
            cached_content=cache_name,
            temperature=0.2,
            safety_settings=_safety_block_none(),
            thinking_config=thinking_config,
        )
    else:
        config = genai_types.GenerateContentConfig(
            system_instruction=sys_instr,
            tools=gemini_tools or None,
            temperature=0.2,
            safety_settings=_safety_block_none(),
            thinking_config=thinking_config,
        )

    if on_delta is not None:
        return _chat_streaming(client, model, contents, config, on_delta)

    resp = client.models.generate_content(
        model=model, contents=contents, config=config
    )
    _log_usage(model, getattr(resp, "usage_metadata", None))
    return _normalize_response(resp)


def _chat_streaming(
    client: genai.Client,
    model: str,
    contents: list[dict],
    config: genai_types.GenerateContentConfig,
    on_delta: Callable[[str], None],
) -> LLMResponse:
    """Consume Gemini's streaming response part-by-part."""
    full_text_parts: list[str] = []
    collected_tool_calls: list[ToolCall] = []
    last_usage: Any = None

    stream = client.models.generate_content_stream(
        model=model, contents=contents, config=config
    )
    for chunk in stream:
        chunk_usage = getattr(chunk, "usage_metadata", None)
        if chunk_usage is not None:
            last_usage = chunk_usage
        candidates = getattr(chunk, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            if content is None:
                continue
            parts = getattr(content, "parts", None) or []
            for part in parts:
                text = getattr(part, "text", None)
                if text:
                    full_text_parts.append(text)
                    on_delta(text)
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    collected_tool_calls.append(
                        ToolCall(name=fc.name, arguments=_coerce_function_call_args(fc))
                    )

    _log_usage(model, last_usage)
    if collected_tool_calls:
        return LLMResponse(kind="tool_calls", tool_calls=collected_tool_calls)
    content = "".join(full_text_parts)
    if not content:
        logger.warning(
            "Gemini stream (model=%s) produced no text and no tool calls", model
        )
    return LLMResponse(kind="message", content=content)


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
