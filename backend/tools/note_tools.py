from __future__ import annotations

import difflib
import logging
import re
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


def _add_note(raw: dict) -> ToolResult:
    args = AddNoteArgs.model_validate(raw)

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
    results = note_service.list_notes(
        tag=args.tag,
        limit=args.limit,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    filters: list[str] = []
    if args.tag:
        filters.append(f"tagged '{args.tag}'")
    if args.date_from or args.date_to:
        rng = (
            f"from {args.date_from.date().isoformat()} " if args.date_from else "up to "
        )
        if args.date_to:
            rng += f"to {args.date_to.date().isoformat()}"
        filters.append(rng.strip())
    msg = (
        f"Found {len(results)} note(s) " + " and ".join(filters) + "."
        if filters
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


_DIGIT_RE = re.compile(r"\d")


def _extract_digit_substitutions(old: str, new: str) -> list[tuple[str, str]]:
    """Token-level replacements where at least one side contains a digit."""
    old_tokens, new_tokens = old.split(), new.split()
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens)
    subs: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op != "replace":
            continue
        old_tok = " ".join(old_tokens[i1:i2])
        new_tok = " ".join(new_tokens[j1:j2])
        if _DIGIT_RE.search(old_tok) or _DIGIT_RE.search(new_tok):
            subs.append((old_tok, new_tok))
    return subs


def _auto_sync_fields(
    current_title: str,
    current_description: str,
    new_title: str,
    new_description: str,
) -> tuple[str, str]:
    """Propagate a numeric edit across title/description when the LLM only updated one."""
    title_changed = current_title != new_title
    desc_changed = current_description != new_description

    if title_changed and not desc_changed:
        for old_tok, new_tok in _extract_digit_substitutions(current_title, new_title):
            if old_tok in new_description:
                new_description = new_description.replace(old_tok, new_tok)

    elif desc_changed and not title_changed:
        for old_tok, new_tok in _extract_digit_substitutions(
            current_description, new_description
        ):
            if old_tok in new_title:
                new_title = new_title.replace(old_tok, new_tok)

    return new_title, new_description


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

    proposed_title = args.title if args.title is not None else current.title
    proposed_description = (
        args.description if args.description is not None else current.description
    )
    synced_title, synced_description = _auto_sync_fields(
        current.title, current.description, proposed_title, proposed_description
    )

    if args.clear_tag:
        new_tag: str | None = None
    elif args.tag is not None:
        new_tag = args.tag
    else:
        new_tag = current.tag

    if not args.confirm:
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
                    "title": synced_title,
                    "description": synced_description,
                    "tag": new_tag,
                }
            },
        )

    commit_title = synced_title if synced_title != proposed_title else args.title
    commit_description = (
        synced_description
        if synced_description != proposed_description
        else args.description
    )
    updated = note_service.update_note(
        args.note_id,
        title=commit_title,
        description=commit_description,
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
