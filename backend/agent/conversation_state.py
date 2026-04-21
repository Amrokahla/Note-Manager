from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from backend.config import settings
from backend.tools.schemas import ToolResult


# Cap message history at 2 * HISTORY_TURNS so a "turn" == one user + one
# assistant (or tool) message pair. Default config → 40 messages, matching
# PLAN §6.3. Going past this risks blowing llama3.2's context window; going
# below it loses the multi-turn awareness that makes "that note" work.
_MAX_MESSAGES = settings.history_turns * 2


@dataclass
class SessionState:
    session_id: str
    messages: deque[dict] = field(
        default_factory=lambda: deque(maxlen=_MAX_MESSAGES)
    )
    last_referenced_note_ids: list[int] = field(default_factory=list)
    pending_confirmation: dict | None = None


class SessionStore:
    """In-memory per-session state. The interface (`get`, `reset`) is small on
    purpose — swapping to Redis later means reimplementing these two methods.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get(self, session_id: str) -> SessionState:
        existing = self._sessions.get(session_id)
        if existing is None:
            existing = SessionState(session_id=session_id)
            self._sessions[session_id] = existing
        return existing

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def clear(self) -> None:
        self._sessions.clear()


def remember_referenced(state: SessionState, result: ToolResult) -> None:
    """Harvest note ids from a ToolResult so the orchestrator can resolve
    pronoun references ("that note", "the last one", "it") on future turns.

    Only updates state when the result actually carries ids. A search that
    returned zero matches or a tool that failed leaves `last_referenced_note_ids`
    alone — the user is probably still talking about whatever they last
    looked at.
    """
    ids = _harvest_ids(result.data)
    if ids:
        state.last_referenced_note_ids = ids


def _harvest_ids(data: object) -> list[int]:
    collected: list[int] = []

    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                rid = row.get("id")
                if isinstance(rid, int):
                    collected.append(rid)
    elif isinstance(data, dict):
        rid = data.get("id")
        if isinstance(rid, int):
            collected.append(rid)
        # delete_note's confirmation response wraps the note under "preview".
        # Capturing that id means "yes, delete it" stays resolvable without
        # the LLM having to echo the number back.
        preview = data.get("preview")
        if isinstance(preview, dict):
            pid = preview.get("id")
            if isinstance(pid, int):
                collected.append(pid)

    # De-dupe while preserving order — a search that returns [3, 5, 3] shouldn't
    # make "that note" ambiguous between duplicates.
    seen: set[int] = set()
    unique: list[int] = []
    for i in collected:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique


def build_context_line(state: SessionState) -> str | None:
    """Produce the hidden '(context)' system turn injected before each user
    message, or None if there's nothing worth telling the model.

    Small-model trick (PLAN §6.2): llama3.2 at 3B tracks pronoun references
    poorly from message history alone. An explicit reminder of the candidate
    ids and any pending confirmation dramatically improves multi-turn behaviour.
    """
    parts: list[str] = []

    if state.last_referenced_note_ids:
        ids = state.last_referenced_note_ids
        primary = ids[0]
        parts.append(
            f"The most recently referenced note ids are: {ids}. "
            f'"that note" / "the last one" / "it" refers to {primary}.'
        )

    if state.pending_confirmation:
        pc = state.pending_confirmation
        tool = pc.get("tool", "<unknown>")
        args = pc.get("args") or {}
        parts.append(
            f"A `{tool}` call is awaiting confirmation with arguments {args}. "
            "Interpret the user's latest message in that context:\n"
            f"  • Affirmative ('yes', 'save it', 'confirm', 'go ahead') → call "
            f"`{tool}` again with the SAME arguments plus confirm=true.\n"
            f"  • Negative ('no', 'cancel', 'never mind') → acknowledge in plain "
            f"text and do NOT call the tool.\n"
            "  • Modification ('use tag X', 'change title to Y', 'different "
            "description') → MERGE the change into the pending arguments and "
            f"call `{tool}` again with confirm=false to re-preview. Do NOT "
            "start a new add/update from scratch — continue the pending one."
        )

    if not parts:
        return None
    return "(context) " + " ".join(parts)
