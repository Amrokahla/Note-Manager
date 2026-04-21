from __future__ import annotations

import logging
from typing import Callable

from pydantic import ValidationError

from backend.services import note_service
from backend.tools.schemas import (
    AddNoteArgs,
    DeleteNoteArgs,
    GetNoteArgs,
    ListRecentArgs,
    SearchNotesArgs,
    SummarizeNotesArgs,
    ToolResult,
    UpdateNoteArgs,
)

logger = logging.getLogger(__name__)


# --- Per-tool handlers -------------------------------------------------------
#
# Each handler takes the raw dict the LLM produced, validates it through a
# Pydantic model, calls the service, and returns a ToolResult. Handlers do NOT
# catch their own exceptions — the top-level execute() wrapper does that so the
# error envelope stays uniform.

def _add_note(raw: dict) -> ToolResult:
    args = AddNoteArgs.model_validate(raw)
    note = note_service.create_note(args.title, args.body, args.tags)
    return ToolResult(
        ok=True,
        message=f"Created note #{note.id} '{note.title}'.",
        data=note.model_dump(mode="json"),
    )


def _search_notes(raw: dict) -> ToolResult:
    args = SearchNotesArgs.model_validate(raw)

    # Ambiguity probe: when the caller asks for "the" match (limit=1), we
    # bump the SQL limit by one so we can actually *detect* multiple matches.
    # Without this, a bare LIMIT 1 would hide ambiguity from us by construction.
    probe_limit = args.limit + 1 if args.limit == 1 else args.limit

    results = note_service.search_notes(
        query=args.query,
        tags=args.tags,
        date_from=args.date_from,
        date_to=args.date_to,
        limit=probe_limit,
    )

    if not results:
        return ToolResult(ok=True, message="No notes matched.", data=[])

    if args.limit == 1 and len(results) > 1:
        return ToolResult(
            ok=False,
            error_code="ambiguous",
            message=(
                f"{len(results)} notes matched. Ask the user which one they mean "
                "instead of guessing."
            ),
            candidates=[r.model_dump(mode="json") for r in results],
        )

    # Trim back to what the caller actually asked for in the happy path.
    results = results[: args.limit]
    return ToolResult(
        ok=True,
        message=f"Found {len(results)} note(s).",
        data=[r.model_dump(mode="json") for r in results],
    )


def _get_note(raw: dict) -> ToolResult:
    args = GetNoteArgs.model_validate(raw)
    note = note_service.get_note(args.note_id)
    if note is None:
        return ToolResult(
            ok=False,
            error_code="not_found",
            message=f"No note with id {args.note_id}.",
        )
    return ToolResult(
        ok=True,
        message=f"Fetched note #{note.id}.",
        data=note.model_dump(mode="json"),
    )


def _update_note(raw: dict) -> ToolResult:
    args = UpdateNoteArgs.model_validate(raw)

    if args.title is None and args.body is None and args.tags is None:
        return ToolResult(
            ok=False,
            error_code="invalid_arg",
            message="Nothing to update — pass at least one of title, body, or tags.",
        )

    updated = note_service.update_note(
        args.note_id, title=args.title, body=args.body, tags=args.tags
    )
    if updated is None:
        return ToolResult(
            ok=False,
            error_code="not_found",
            message=f"No note with id {args.note_id}.",
        )
    return ToolResult(
        ok=True,
        message=f"Updated note #{updated.id}.",
        data=updated.model_dump(mode="json"),
    )


def _delete_note(raw: dict) -> ToolResult:
    args = DeleteNoteArgs.model_validate(raw)
    note = note_service.get_note(args.note_id)
    if note is None:
        return ToolResult(
            ok=False,
            error_code="not_found",
            message=f"No note with id {args.note_id}.",
        )

    # Two-step gate. The service is willing to delete unconditionally; the
    # dispatcher is the trust boundary that refuses to call it without confirm.
    if not args.confirm:
        return ToolResult(
            ok=False,
            needs_confirmation=True,
            error_code="needs_confirmation",
            message=(
                f"About to delete note #{note.id} '{note.title}'. "
                "Ask the user to confirm, then call delete_note again with confirm=true."
            ),
            data={"preview": note.model_dump(mode="json")},
        )

    note_service.delete_note(args.note_id)
    return ToolResult(ok=True, message=f"Deleted note #{args.note_id}.")


def _list_recent(raw: dict) -> ToolResult:
    args = ListRecentArgs.model_validate(raw)
    results = note_service.list_recent(limit=args.limit)
    return ToolResult(
        ok=True,
        message=f"Listed {len(results)} recent note(s).",
        data=[r.model_dump(mode="json") for r in results],
    )


def _summarize_notes(raw: dict) -> ToolResult:
    args = SummarizeNotesArgs.model_validate(raw)
    found: list[dict] = []
    missing: list[int] = []
    for nid in args.note_ids:
        n = note_service.get_note(nid)
        if n is None:
            missing.append(nid)
        else:
            found.append(n.model_dump(mode="json"))

    if not found:
        return ToolResult(
            ok=False,
            error_code="not_found",
            message=f"None of those ids exist: {missing}.",
        )

    message = f"Fetched {len(found)} note(s)."
    if missing:
        message += f" Missing: {missing}."
    return ToolResult(ok=True, message=message, data=found)


_HANDLERS: dict[str, Callable[[dict], ToolResult]] = {
    "add_note": _add_note,
    "search_notes": _search_notes,
    "get_note": _get_note,
    "update_note": _update_note,
    "delete_note": _delete_note,
    "list_recent": _list_recent,
    "summarize_notes": _summarize_notes,
}


def execute(name: str, raw_args: dict | None) -> ToolResult:
    """Run a tool and return a ToolResult. Never raises.

    The orchestrator feeds this directly back to the LLM, so any exception
    leaking out would break the loop. We catch ValidationError explicitly to
    get clean "invalid_arg" envelopes, and everything else funnels into
    "internal" with the exception type name for lightweight debugging.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolResult(
            ok=False,
            error_code="invalid_arg",
            message=f"Unknown tool: {name!r}.",
        )
    try:
        return handler(raw_args or {})
    except ValidationError as e:
        return ToolResult(
            ok=False,
            error_code="invalid_arg",
            message=f"Invalid arguments for {name}: {e.errors(include_url=False)}",
        )
    except Exception as e:
        logger.exception("Tool %s raised an unexpected exception", name)
        return ToolResult(
            ok=False,
            error_code="internal",
            message=f"Internal error in {name}: {type(e).__name__}: {e}",
        )
