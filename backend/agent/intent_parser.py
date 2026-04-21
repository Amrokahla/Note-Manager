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
    """A single tool call executed during one user turn, with its result.
    The HTTP layer serializes these for the frontend's tool-panel (FRONTEND_PLAN §4.1).
    """

    id: str
    name: str
    arguments: dict
    result: ToolResult


@dataclass
class TurnResult:
    reply: str
    tool_calls: list[TurnToolCall] = field(default_factory=list)


# Emit callback used by the SSE streaming endpoint to surface each stage of
# the loop in real time. The string keys match FRONTEND_PLAN §4.2 verbatim so
# the server's `event:` line is the same token the frontend switches on.
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


# --- Intent gate ------------------------------------------------------------
#
# llama3.2 at 3B can't resist firing a tool when tools are in its context,
# even for "hi". The system prompt steers the reply text but not the decision
# to call. To fix that at the source we simply don't GIVE the model tools when
# the user's input clearly isn't a note operation — `tools=[]` removes the
# temptation entirely.
#
# Heuristic: look for a note-related keyword token. We err toward "note op"
# when uncertain (false negatives = missed tool calls the user has to rephrase
# for; false positives = a chatty reply plus maybe one spurious tool call that
# the prompt rules still moderate). That's the cheaper failure mode.

_NOTE_KEYWORD_PATTERN = re.compile(
    r"\b("
    # Write / mutate
    r"note|notes|notebook|notepad|jot|save|store|record|write|writes|wrote|"
    r"add|adds|added|create|creates|created|"
    r"remember|rememb|reminder|reminders|todo|to-do|task|tasks|"
    r"update|updates|updated|edit|edits|edited|change|changes|changed|modify|modified|"
    r"append|appends|amend|rename|renamed|"
    r"delete|deletes|deleted|remove|removes|removed|clear|clears|drop|drops|trash|"
    # Tag vocabulary (for list_tags / list_notes by tag)
    r"tag|tags|tagged|untag|untagged|category|categories|"
    # Read
    r"show|shows|showed|list|lists|listed|recent|find|finds|found|search|searches|"
    r"searched|look|looks|looked|looking|lookup|get|gets|fetch|fetched|open|opens|"
    r"read|reads|summar[iy]s?e|summar[iy]sed|summary|summaries|recall|recalled"
    r")\b",
    re.IGNORECASE,
)


def looks_like_note_op(text: str) -> bool:
    """Return True if `text` plausibly asks for a note operation.

    When False, the orchestrator sends the LLM a tools=[] payload so the model
    is forced to respond as plain chat. The system prompt still steers the
    reply shape (warm greeting / polite refusal).
    """
    return bool(_NOTE_KEYWORD_PATTERN.search(text))


# One store per process. The SessionStore interface is tiny on purpose so we
# can swap this for a Redis-backed implementation later without touching the
# orchestrator — see PLAN §6.1.
store: SessionStore = SessionStore()

_FALLBACK_REPLY = (
    "I'm having trouble completing that — could you rephrase or break it into "
    "smaller steps?"
)


def _build_messages(state: SessionState) -> list[dict]:
    """Assemble the message list sent to the LLM.

    Order matters: system prompt first, ephemeral context line next (so the
    model sees pronoun targets before it reads history), then the rolling
    deque of user/assistant/tool messages.
    """
    out: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    ctx = build_context_line(state)
    if ctx:
        out.append({"role": "system", "content": ctx})
    out.extend(state.messages)
    return out


_MERGEABLE_TOOLS = {"add_note", "update_note"}

# Words that signal the user wants to commit a pending add/update RIGHT NOW
# — paired with the pending confirmation, we force confirm=true regardless of
# what the model sent. This is a belt-and-suspenders guard against the 8B
# failure mode where the model sets confirm=false on compound commands like
# "tag it X and save it".
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
    """When there's an in-flight needs_confirmation for the same tool, merge
    the new args onto the stored pending args. This defends against the 8B
    model's habit of sending only the diff on a modify turn (e.g. "make the
    tag meetings and save it" arriving as title="" description="" tag="meetings"
    after the full title + description were already previewed).

    Rules:
      • Only mergeable tools (add_note, update_note) — search/list/get are stateless.
      • Tool name must match the pending tool.
      • New args overwrite only when they are "real values" — None and empty
        strings don't clobber a prior value.
    """
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
    # Reset confirm to False for a fresh preview by default; caller may override
    # via force_confirm when the user's message signals commit intent.
    merged["confirm"] = bool(call.arguments.get("confirm") or force_confirm)
    return merged


def _sanitize_args(args: dict) -> dict:
    """Remove empty-string values so they behave as 'field omitted' at the
    schema layer. Models often emit `title: ""` when they mean 'don't touch
    this field' — without this pass those would fail min_length validation."""
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
    # Strip empty strings first so the merge and validation see only real
    # values. 8B models sometimes send title="" / description="" to mean
    # "I didn't compute a new value" — treat that as omission.
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

    # Pending-confirmation state machine: set when a gated tool asks for
    # confirmation, cleared the moment any non-gated tool runs. We stash the
    # MERGED args so that a subsequent modify turn picks up the complete
    # picture rather than just what the last LLM call sent.
    if result.needs_confirmation:
        state.pending_confirmation = {"tool": call.name, "args": effective_args}
    else:
        state.pending_confirmation = None

    # Persist the assistant's tool call AND the tool response into history so
    # subsequent hops (and future turns) see the full trace. Record the
    # merged args so history reflects what actually hit the dispatcher.
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
    """Run one user turn through the agent loop.

    Returns a TurnResult (used by the non-streaming `/chat` endpoint and tests).
    If `emit` is provided, also pushes progressive events to it — used by the
    SSE `/chat/stream` endpoint so the UI can animate tool cards as each tool
    is dispatched. Event names match FRONTEND_PLAN §4.2 exactly.

    Bounded by settings.max_tool_hops — if the LLM never emits a plain message
    within that many hops we return a fallback reply, never an infinite loop.
    """
    _emit: EmitFunc = emit or (lambda _t, _d: None)

    state = store.get(session_id)
    state.messages.append({"role": "user", "content": user_text})
    _emit("user_echo", {"message": user_text})

    # Intent gate: decide *once per turn* whether to expose tools to the LLM.
    # If the message doesn't look like a note operation we pass tools=[] so
    # the model physically can't fire a tool — kills 3B's reflexive tool use
    # on greetings and off-topic chat.
    allow_tools = looks_like_note_op(user_text) or bool(state.pending_confirmation)
    tools_for_turn: list[dict] | None = None if allow_tools else []
    if not allow_tools:
        logger.info(
            "Intent gate: tools disabled for session %s (message=%r)",
            session_id,
            user_text,
        )

    # Commit-intent gate: if the user's message signals "commit now" during
    # an active add/update confirmation, force confirm=true downstream.
    # Defense against the LLM setting confirm=false on compound commands like
    # "tag it X and save" — we can't rely on prompt compliance at 8B.
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

        # Stream content tokens as they arrive. For tool-call hops the stream
        # typically yields nothing before the final chunk — no harm done.
        # When the response turns out to be a plain message the user sees it
        # token-by-token instead of as one block at the end.
        def _forward_delta(delta: str) -> None:
            _emit("assistant_delta", {"content": delta})

        resp = llm_handler.chat(
            messages,
            tools=tools_for_turn,
            on_delta=_forward_delta,
            model=model,
        )

        if resp.kind == "message":
            reply = resp.content or ""
            state.messages.append({"role": "assistant", "content": reply})
            _emit("assistant", {"content": reply})
            _emit("done", {})
            return TurnResult(reply=reply, tool_calls=turn_calls)

        if not resp.tool_calls:
            logger.warning("Empty tool_calls from LLM on session %s", session_id)
            break

        for call in resp.tool_calls:
            tc_id = f"tc-{uuid.uuid4().hex[:8]}"
            _emit(
                "tool_call",
                {
                    "id": tc_id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "status": "running",
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


# --- Manual verification CLI (PLAN §7.4) ------------------------------------
#
# Run:  python -m backend.agent.intent_parser
# Requires a running Ollama with llama3.2 pulled. Not part of the test suite.

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
