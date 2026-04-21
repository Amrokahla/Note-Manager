from __future__ import annotations

import logging

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


def handle_user_message(session_id: str, user_text: str) -> str:
    """Run one user turn through the agent loop and return the assistant's reply.

    Bounded by settings.max_tool_hops. If the LLM never emits a plain message
    within that many hops we return a generic fallback — never an infinite loop.
    """
    state = store.get(session_id)
    state.messages.append({"role": "user", "content": user_text})

    for _ in range(settings.max_tool_hops):
        messages = _build_messages(state)
        resp = llm_handler.chat(messages)

        if resp.kind == "message":
            reply = resp.content or ""
            state.messages.append({"role": "assistant", "content": reply})
            return reply

        if not resp.tool_calls:
            # kind=tool_calls with an empty list — nothing to execute and nothing
            # to return; break to the fallback rather than spin.
            logger.warning("Empty tool_calls from LLM on session %s", session_id)
            break

        for call in resp.tool_calls:
            _run_tool_call(state, call)

    logger.warning("Hit MAX_TOOL_HOPS on session %s", session_id)
    state.messages.append({"role": "assistant", "content": _FALLBACK_REPLY})
    return _FALLBACK_REPLY


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
        reply = handle_user_message(session, text)
        print(f"bot > {reply}\n")
