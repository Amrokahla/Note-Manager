from __future__ import annotations

import logging
from typing import Callable

from pydantic import ValidationError

from backend.services import note_service
from backend.tools.schemas import (
    AddNoteArgs,
    DeleteNoteArgs,
    GetNoteArgs,
    ListNotesArgs,
    ListTagsArgs,
    SearchNotesArgs,
    ToolResult,
    UpdateNoteArgs,
)

logger = logging.getLogger(__name__)


# --- Per-tool handlers -----------------------------------------------------

def _add_note(raw: dict) -> ToolResult:
    args = AddNoteArgs.model_validate(raw)

    # Two-step gate (same pattern as delete_note). First call → preview;
    # only the confirm=true second call actually writes. Prevents the model
    # from committing a note before the user has verified the fields.
    if not args.confirm:
        return ToolResult(
            ok=False,
            needs_confirmation=True,
            error_code="needs_confirmation",
            message=(
                f"About to save this note — title '{args.title}', tag "
                f"'{args.tag or 'none'}'. Show this preview to the user and "
                "ask them to confirm before calling add_note again with confirm=true."
            ),
            data={
                "preview": {
                    "title": args.title,
                    "description": args.description,
                    "tag": args.tag,
                }
            },
        )

    note = note_service.create_note(args.title, args.description, args.tag)
    return ToolResult(
        ok=True,
        message=f"Created note #{note.id} '{note.title}'.",
        data=note.model_dump(mode="json"),
    )


def _list_notes(raw: dict) -> ToolResult:
    args = ListNotesArgs.model_validate(raw)
    results = note_service.list_notes(tag=args.tag, limit=args.limit)
    msg = (
        f"Found {len(results)} note(s) tagged '{args.tag}'."
        if args.tag
        else f"Listed {len(results)} recent note(s)."
    )
    return ToolResult(
        ok=True,
        message=msg,
        data=[r.model_dump(mode="json") for r in results],
    )


def _list_tags(raw: dict) -> ToolResult:
    args = ListTagsArgs.model_validate(raw)
    tags = note_service.list_tags(limit=args.limit)
    return ToolResult(
        ok=True,
        message=f"Top {len(tags)} tag(s).",
        data=[t.model_dump(mode="json") for t in tags],
    )


def _search_notes(raw: dict) -> ToolResult:
    args = SearchNotesArgs.model_validate(raw)
    try:
        results, above = note_service.search_semantic(
            query=args.query, limit=args.limit
        )
    except ValueError as e:
        return ToolResult(ok=False, error_code="invalid_arg", message=str(e))

    if not results:
        return ToolResult(
            ok=True,
            message="No notes at all (or none have embeddings yet).",
            data=[],
        )

    data = [r.model_dump(mode="json") for r in results]

    if above:
        # Confident match(es). If >1, surface as candidates so the model
        # disambiguates with the user rather than picking itself.
        if len(results) > 1:
            return ToolResult(
                ok=True,
                message=f"Found {len(results)} matching note(s) — ask the user which one.",
                data=data,
                candidates=data,
            )
        return ToolResult(
            ok=True,
            message=f"Found {len(results)} matching note.",
            data=data,
        )

    # Best-effort fallback: nothing beat the threshold, but the user may still
    # want to see the closest few. The prompt tells the LLM to acknowledge
    # "no strong match" and present these as low-confidence options.
    return ToolResult(
        ok=True,
        message=(
            "No strong match (nothing above similarity threshold). "
            f"Here are the closest {len(results)} note(s) as a best-effort fallback — "
            "tell the user no exact match was found and show them as possibilities."
        ),
        data=data,
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
    if (
        args.title is None
        and args.description is None
        and args.tag is None
        and not args.clear_tag
    ):
        return ToolResult(
            ok=False,
            error_code="invalid_arg",
            message="Nothing to update — pass at least one of title, description, tag, or clear_tag.",
        )

    current = note_service.get_note(args.note_id)
    if current is None:
        return ToolResult(
            ok=False,
            error_code="not_found",
            message=f"No note with id {args.note_id}.",
        )

    # Two-step gate — preview the merged fields BEFORE committing.
    if not args.confirm:
        new_tag: str | None
        if args.clear_tag:
            new_tag = None
        elif args.tag is not None:
            new_tag = args.tag
        else:
            new_tag = current.tag
        return ToolResult(
            ok=False,
            needs_confirmation=True,
            error_code="needs_confirmation",
            message=(
                f"About to update note #{current.id}. Show this preview to the "
                "user and ask them to confirm before calling update_note again "
                "with confirm=true."
            ),
            data={
                "preview": {
                    "id": current.id,
                    "title": args.title if args.title is not None else current.title,
                    "description": (
                        args.description
                        if args.description is not None
                        else current.description
                    ),
                    "tag": new_tag,
                }
            },
        )

    updated = note_service.update_note(
        args.note_id,
        title=args.title,
        description=args.description,
        tag=args.tag,
        clear_tag=args.clear_tag,
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

    if not args.confirm:
        return ToolResult(
            ok=False,
            needs_confirmation=True,
            error_code="needs_confirmation",
            message=(
                f"About to permanently delete note #{note.id} '{note.title}'. "
                "Ask the user to confirm before calling delete_note again with confirm=true."
            ),
            data={"preview": note.model_dump(mode="json")},
        )

    note_service.delete_note(args.note_id)
    return ToolResult(ok=True, message=f"Deleted note #{args.note_id}.")


_HANDLERS: dict[str, Callable[[dict], ToolResult]] = {
    "add_note": _add_note,
    "list_notes": _list_notes,
    "list_tags": _list_tags,
    "search_notes": _search_notes,
    "get_note": _get_note,
    "update_note": _update_note,
    "delete_note": _delete_note,
}


def execute(name: str, raw_args: dict | None) -> ToolResult:
    """Run a tool and return a ToolResult. Never raises."""
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
