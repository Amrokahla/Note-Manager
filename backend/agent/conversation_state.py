from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from backend.config import settings
from backend.tools.schemas import ToolResult


_MAX_MESSAGES = settings.history_turns * 2


@dataclass
class SessionState:
    session_id: str
    user_id: int | None = None
    messages: deque[dict] = field(
        default_factory=lambda: deque(maxlen=_MAX_MESSAGES)
    )
    last_referenced_note_ids: list[int] = field(default_factory=list)
    pending_confirmation: dict | None = None


def _compound_key(user_id: int | None, session_id: str) -> str:
    """Flat compound key so two users with the same session_id don't collide."""
    # `None` is only used by pre-auth call sites (tests, CLI harness).
    return f"{user_id if user_id is not None else '-'}:{session_id}"


class SessionStore:
    """In-memory per-user, per-session state."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get(
        self, session_id: str, *, user_id: int | None = None
    ) -> SessionState:
        key = _compound_key(user_id, session_id)
        existing = self._sessions.get(key)
        if existing is None:
            existing = SessionState(session_id=session_id, user_id=user_id)
            self._sessions[key] = existing
        return existing

    def reset(self, session_id: str, *, user_id: int | None = None) -> None:
        self._sessions.pop(_compound_key(user_id, session_id), None)

    def reset_user(self, user_id: int) -> None:
        prefix = f"{user_id}:"
        for k in [k for k in self._sessions if k.startswith(prefix)]:
            self._sessions.pop(k, None)

    def clear(self) -> None:
        self._sessions.clear()


def remember_referenced(state: SessionState, result: ToolResult) -> None:
    """Harvest note ids from a ToolResult to resolve later pronoun references."""
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
        preview = data.get("preview")
        if isinstance(preview, dict):
            pid = preview.get("id")
            if isinstance(pid, int):
                collected.append(pid)

    seen: set[int] = set()
    unique: list[int] = []
    for i in collected:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique


def build_context_line(state: SessionState) -> str | None:
    """Build the hidden '(context)' system turn injected before each user message."""
    parts: list[str] = []

    now = datetime.now().astimezone()
    parts.append(
        f'Today is {now.strftime("%A, %B %d, %Y")} (local time).'
    )

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
