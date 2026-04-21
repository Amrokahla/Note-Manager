from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

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


def handle_user_message(session_id: str, user_text: str) -> TurnResult:
    """Run one user turn through the agent loop.

    Returns a TurnResult with the assistant's reply and every tool call that
    fired during this turn (so the HTTP layer can render them in the UI's
    tool panel). Bounded by settings.max_tool_hops — if the LLM never emits a
    plain message within that many hops we return a fallback reply, never an
    infinite loop.
    """
    state = store.get(session_id)
    state.messages.append({"role": "user", "content": user_text})
    turn_calls: list[TurnToolCall] = []

    for _ in range(settings.max_tool_hops):
        messages = _build_messages(state)
        resp = llm_handler.chat(messages)

        if resp.kind == "message":
            reply = resp.content or ""
            state.messages.append({"role": "assistant", "content": reply})
            return TurnResult(reply=reply, tool_calls=turn_calls)

        if not resp.tool_calls:
            logger.warning("Empty tool_calls from LLM on session %s", session_id)
            break

        for call in resp.tool_calls:
            result = _run_tool_call(state, call)
            turn_calls.append(
                TurnToolCall(
                    id=f"tc-{uuid.uuid4().hex[:8]}",
                    name=call.name,
                    arguments=call.arguments,
                    result=result,
                )
            )

    logger.warning("Hit MAX_TOOL_HOPS on session %s", session_id)
    state.messages.append({"role": "assistant", "content": _FALLBACK_REPLY})
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
