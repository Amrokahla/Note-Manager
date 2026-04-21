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


def _run_tool_call(state: SessionState, call: ToolCall) -> ToolResult:
    """Dispatch one tool call and record everything the next hop needs to see."""
    result = note_tools.execute(call.name, call.arguments)
    remember_referenced(state, result)

    # Pending-confirmation state machine: set when a destructive tool asks
    # for confirmation, cleared the moment any non-gated tool runs.
    if result.needs_confirmation:
        state.pending_confirmation = {"tool": call.name, "args": call.arguments}
    else:
        state.pending_confirmation = None

    # Persist the assistant's tool call AND the tool response into history so
    # subsequent hops (and future turns) see the full trace.
    state.messages.append(
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": call.name, "arguments": call.arguments}}
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

    turn_calls: list[TurnToolCall] = []

    for _ in range(settings.max_tool_hops):
        messages = _build_messages(state)
        resp = llm_handler.chat(messages, tools=tools_for_turn)

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
            result = _run_tool_call(state, call)
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
