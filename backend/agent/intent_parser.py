from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.agent import llm_handler
from backend.agent.conversation_state import (
    SessionState,
    SessionStore,
    build_context_line,
    remember_referenced,
)
from backend.agent.llm_handler import ToolCall
from backend.agent.prompts import SYSTEM_PROMPT
from backend.config import settings
from backend.tools import note_tools
from backend.tools.schemas import ToolResult

logger = logging.getLogger(__name__)


@dataclass
class TurnToolCall:
    """One tool call executed during a user turn, with its result."""

    id: str
    name: str
    arguments: dict
    result: ToolResult


@dataclass
class TurnResult:
    reply: str
    tool_calls: list[TurnToolCall] = field(default_factory=list)


EmitFunc = Callable[[str, dict], None]


def _result_status(result: ToolResult) -> str:
    if result.needs_confirmation:
        return "needs_confirmation"
    if result.ok:
        return "ok"
    return "fail"


def _result_payload(tc_id: str, result: ToolResult) -> dict[str, Any]:
    return {
        "id": tc_id,
        "status": _result_status(result),
        "message": result.message,
        "error_code": result.error_code,
        "data": result.data,
        "needs_confirmation": result.needs_confirmation,
        "candidates": result.candidates,
    }


_NOTE_KEYWORD_PATTERN = re.compile(
    r"\b("
    r"note|notes|notebook|notepad|jot|save|store|record|write|writes|wrote|"
    r"add|adds|added|create|creates|created|"
    r"remember|rememb|reminder|reminders|todo|to-do|task|tasks|"
    r"update|updates|updated|edit|edits|edited|change|changes|changed|modify|modified|"
    r"append|appends|amend|rename|renamed|"
    r"delete|deletes|deleted|remove|removes|removed|clear|clears|drop|drops|trash|"
    r"tag|tags|tagged|untag|untagged|category|categories|"
    r"show|shows|showed|list|lists|listed|recent|find|finds|found|search|searches|"
    r"searched|look|looks|looked|looking|lookup|get|gets|fetch|fetched|open|opens|"
    r"read|reads|summar[iy]s?e|summar[iy]sed|summary|summaries|recall|recalled|"
    r"meeting|meetings|appointment|appointments|schedule|agenda|calendar|"
    r"today|tomorrow|yesterday|tonight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"morning|afternoon|evening"
    r")\b",
    re.IGNORECASE,
)


def looks_like_note_op(text: str) -> bool:
    """True if `text` plausibly asks for a note operation; else orchestrator passes tools=[]."""
    return bool(_NOTE_KEYWORD_PATTERN.search(text))


_INTENT_CLASSIFIER_SYSTEM = (
    "You are a binary classifier for a note-taking assistant. Decide whether "
    "the user's message is asking to perform a note operation — adding, "
    "updating, deleting, searching, listing, tagging, or referencing a note — "
    "or is something else (greeting, small talk, meta question, off-topic). "
    'Reply with exactly one lowercase word: "note_op" or "other". '
    "No punctuation, no preamble, no explanation."
)


def _classify_intent_llm(user_text: str, model: str) -> bool:
    """LLM-based intent gate for providers reliable enough to run one.

    On any transport error or unrecognized reply, falls back to the keyword
    regex so the gate never fails-open into silence.
    """
    try:
        resp = llm_handler.chat(
            messages=[
                {"role": "system", "content": _INTENT_CLASSIFIER_SYSTEM},
                {"role": "user", "content": user_text},
            ],
            tools=[],
            on_delta=None,
            model=model,
        )
    except Exception as e:
        logger.warning(
            "Intent classifier failed (%s) — falling back to regex", e
        )
        return looks_like_note_op(user_text)

    text = (resp.content or "").strip().lower()
    if text.startswith("note"):
        return True
    if text.startswith("other"):
        return False
    logger.warning(
        "Intent classifier returned unrecognized %r — falling back to regex",
        text,
    )
    return looks_like_note_op(user_text)


def _gate_allow_tools(user_text: str, model: str, state: SessionState) -> bool:
    """Decide whether to expose tools to the LLM this turn.

    • A pending confirmation always overrides (the user's 'yes/no/modify'
      must be able to reach the tool).
    • Gemini uses an LLM classifier — smarter, paid with one extra small call.
    • Ollama uses the regex — small models can't be trusted with a classifier
      call AND tool-enabled main call on the same prompt.
    """
    if state.pending_confirmation:
        return True
    provider = llm_handler.MODEL_OPTIONS.get(model, ("ollama", None))[0]
    if provider == "gemini":
        return _classify_intent_llm(user_text, model)
    return looks_like_note_op(user_text)


store: SessionStore = SessionStore()

_FALLBACK_REPLY = (
    "I'm having trouble completing that — could you rephrase or break it into "
    "smaller steps?"
)


def _build_messages(state: SessionState) -> list[dict]:
    """System prompt, then context line, then the rolling message deque."""
    out: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    ctx = build_context_line(state)
    if ctx:
        out.append({"role": "system", "content": ctx})
    out.extend(state.messages)
    return out


_MERGEABLE_TOOLS = {"add_note", "update_note"}

_COMMIT_INTENT_PATTERN = re.compile(
    r"\b(save|saving|save\s+it|create|creating|create\s+it|commit|"
    r"confirm|confirmed|add\s+it|do\s+it|go\s+ahead|yes|yeah|yep|"
    r"ok(ay)?|sure)\b",
    re.IGNORECASE,
)


def _looks_like_commit_intent(user_text: str) -> bool:
    return bool(_COMMIT_INTENT_PATTERN.search(user_text))


def _merge_with_pending(
    call: ToolCall,
    state: SessionState,
    *,
    force_confirm: bool = False,
) -> dict:
    """Merge new args onto a pending add/update preview; empty values don't clobber."""
    if call.name not in _MERGEABLE_TOOLS:
        return dict(call.arguments)
    pc = state.pending_confirmation
    if not pc or pc.get("tool") != call.name:
        return dict(call.arguments)

    merged: dict = dict(pc.get("args") or {})
    for key, value in call.arguments.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value
    merged["confirm"] = bool(call.arguments.get("confirm") or force_confirm)
    return merged


def _sanitize_args(args: dict) -> dict:
    """Drop empty-string values so they act as 'field omitted' at the schema layer."""
    return {
        k: v
        for k, v in args.items()
        if not (isinstance(v, str) and not v.strip())
    }


def _run_tool_call(
    state: SessionState,
    call: ToolCall,
    *,
    force_confirm: bool = False,
) -> ToolResult:
    """Dispatch one tool call and record everything the next hop needs to see."""
    sanitized = ToolCall(name=call.name, arguments=_sanitize_args(call.arguments))
    effective_args = _merge_with_pending(
        sanitized, state, force_confirm=force_confirm
    )
    if effective_args != call.arguments:
        logger.info(
            "Merged pending args for %s: %s → %s",
            call.name,
            call.arguments,
            effective_args,
        )

    result = note_tools.execute(call.name, effective_args)
    remember_referenced(state, result)

    if result.needs_confirmation:
        state.pending_confirmation = {"tool": call.name, "args": effective_args}
    else:
        state.pending_confirmation = None

    state.messages.append(
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": call.name, "arguments": effective_args}}
            ],
        }
    )
    state.messages.append(
        {
            "role": "tool",
            "name": call.name,
            "content": result.model_dump_json(),
        }
    )
    return result


def handle_user_message(
    session_id: str,
    user_text: str,
    emit: EmitFunc | None = None,
    *,
    model: str = llm_handler.DEFAULT_MODEL,
) -> TurnResult:
    """Run one user turn through the agent loop, bounded by settings.max_tool_hops."""
    _emit: EmitFunc = emit or (lambda _t, _d: None)

    state = store.get(session_id)
    state.messages.append({"role": "user", "content": user_text})
    _emit("user_echo", {"message": user_text})

    allow_tools = _gate_allow_tools(user_text, model, state)
    tools_for_turn: list[dict] | None = None if allow_tools else []
    if not allow_tools:
        logger.info(
            "Intent gate: tools disabled for session %s (message=%r)",
            session_id,
            user_text,
        )

    force_confirm = bool(
        state.pending_confirmation
        and state.pending_confirmation.get("tool") in _MERGEABLE_TOOLS
        and _looks_like_commit_intent(user_text)
    )
    if force_confirm:
        logger.info(
            "Commit-intent detected for session %s — forcing confirm=true",
            session_id,
        )

    turn_calls: list[TurnToolCall] = []

    for _ in range(settings.max_tool_hops):
        messages = _build_messages(state)

        def _forward_delta(delta: str) -> None:
            _emit("assistant_delta", {"content": delta})

        try:
            resp = llm_handler.chat(
                messages,
                tools=tools_for_turn,
                on_delta=_forward_delta,
                model=model,
            )
        except Exception as e:
            logger.warning(
                "Provider error from model=%s session=%s (%s) — retrying once",
                model,
                session_id,
                e,
            )
            resp = llm_handler.chat(
                messages,
                tools=tools_for_turn,
                on_delta=None,
                model=model,
            )

        if resp.kind == "message" and not (resp.content or "").strip():
            logger.warning(
                "Empty response from model=%s session=%s — retrying once",
                model,
                session_id,
            )
            resp = llm_handler.chat(
                messages,
                tools=tools_for_turn,
                on_delta=None,
                model=model,
            )

        if resp.kind == "message":
            reply = (resp.content or "").strip()
            if not reply:
                logger.warning(
                    "Still empty after retry from model=%s session=%s — using fallback",
                    model,
                    session_id,
                )
                reply = "Sorry, I didn't quite catch that. Could you rephrase?"
            state.messages.append({"role": "assistant", "content": reply})
            _emit("assistant", {"content": reply})
            _emit("done", {})
            return TurnResult(reply=reply, tool_calls=turn_calls)

        if not resp.tool_calls:
            logger.warning("Empty tool_calls from LLM on session %s", session_id)
            break

        for call in resp.tool_calls:
            tc_id = f"tc-{uuid.uuid4().hex[:8]}"
            continues_pending = (
                state.pending_confirmation is not None
                and state.pending_confirmation.get("tool") == call.name
            )
            _emit(
                "tool_call",
                {
                    "id": tc_id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "status": "running",
                    "continues_pending": continues_pending,
                },
            )
            result = _run_tool_call(state, call, force_confirm=force_confirm)
            _emit("tool_result", _result_payload(tc_id, result))
            turn_calls.append(
                TurnToolCall(
                    id=tc_id,
                    name=call.name,
                    arguments=call.arguments,
                    result=result,
                )
            )

    logger.warning("Hit MAX_TOOL_HOPS on session %s", session_id)
    state.messages.append({"role": "assistant", "content": _FALLBACK_REPLY})
    _emit("assistant", {"content": _FALLBACK_REPLY})
    _emit("done", {})
    return TurnResult(reply=_FALLBACK_REPLY, tool_calls=turn_calls)


if __name__ == "__main__":  # pragma: no cover
    import uuid

    from backend.db.sqlite import init_db

    init_db()
    session = str(uuid.uuid4())
    print(f"Note Agent CLI — session {session[:8]}. Ctrl-D or 'exit' to quit.\n")
    while True:
        try:
            text = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in {"exit", "quit"}:
            break
        result = handle_user_message(session, text)
        print(f"bot > {result.reply}\n")
        for tc in result.tool_calls:
            status = "ok" if tc.result.ok else (tc.result.error_code or "fail")
            print(f"      [{status}] {tc.name}({tc.arguments})")
        if result.tool_calls:
            print()
