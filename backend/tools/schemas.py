from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field


def _coerce_json_list(v: Any) -> Any:
    """Accept a JSON-encoded string as a list.

    Small models sometimes emit list arguments as a JSON-encoded string
    (e.g. `"[\\"work\\"]"`). Rather than fail the tool + retry, we parse the
    string back into the list the model intended. Non-string or non-JSON
    input is returned unchanged so the normal validators still run.
    """
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return v
        if isinstance(parsed, list):
            return parsed
    return v


StrList = Annotated[list[str], BeforeValidator(_coerce_json_list)]


# --- Tool argument models ---------------------------------------------------

class AddNoteArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    tag: str | None = Field(default=None, max_length=50)
    confirm: bool = Field(
        default=False,
        description=(
            "Must be true for the save to commit. First call with confirm=false "
            "to get a preview + needs_confirmation response; only call again with "
            "confirm=true after the user has explicitly agreed."
        ),
    )


class ListNotesArgs(BaseModel):
    tag: str | None = Field(
        default=None,
        description="Optional tag filter. When set, returns notes with this tag only.",
    )
    limit: int = Field(default=10, ge=1, le=50)


class ListTagsArgs(BaseModel):
    limit: int = Field(default=4, ge=1, le=20)


class SearchNotesArgs(BaseModel):
    query: str = Field(..., min_length=1, description="Natural-language query for semantic search.")
    limit: int = Field(default=5, ge=1, le=20)


class GetNoteArgs(BaseModel):
    note_id: int = Field(..., ge=1)


class UpdateNoteArgs(BaseModel):
    note_id: int = Field(..., ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1)
    tag: str | None = Field(default=None, max_length=50)
    clear_tag: bool = Field(
        default=False,
        description="Set true to explicitly remove the note's tag (distinct from 'leave tag alone').",
    )
    confirm: bool = Field(
        default=False,
        description=(
            "Must be true for the patch to commit. First call with confirm=false "
            "to get a preview of the updated fields + needs_confirmation response; "
            "only call again with confirm=true after the user has explicitly agreed."
        ),
    )


class DeleteNoteArgs(BaseModel):
    note_id: int = Field(..., ge=1)
    confirm: bool = Field(
        default=False,
        description=(
            "Must be true for deletion to proceed. First call with confirm=false "
            "to preview; only pass confirm=true after the user has explicitly agreed."
        ),
    )


# --- Uniform tool result envelope ------------------------------------------

ErrorCode = Literal[
    "not_found",
    "invalid_arg",
    "ambiguous",
    "needs_confirmation",
    "internal",
]


class ToolResult(BaseModel):
    ok: bool
    message: str
    data: Any | None = None
    needs_confirmation: bool = False
    candidates: list[dict] | None = None
    error_code: ErrorCode | None = None


# --- Ollama tool descriptors -----------------------------------------------

def _tool(name: str, description: str, args_model: type[BaseModel]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": args_model.model_json_schema(),
        },
    }


TOOL_DEFS: list[dict] = [
    _tool(
        "add_note",
        (
            "Save a NEW note after the user has explicitly confirmed. "
            "`title` and `description` are REQUIRED non-empty strings. `tag` "
            "is OPTIONAL — omit it rather than guessing. Only call this tool "
            "after presenting the proposed fields to the user in plain text "
            "and receiving an affirmative confirmation ('yes', 'save it', "
            "'confirm', etc.). Never call with empty or placeholder values."
        ),
        AddNoteArgs,
    ),
    _tool(
        "list_notes",
        (
            "List recent notes, optionally filtered by tag. Use for 'show my "
            "notes', 'list all notes', 'what notes do I have', 'notes tagged "
            "X'. Default limit=10, max=50. Pass `tag` to filter; omit for all."
        ),
        ListNotesArgs,
    ),
    _tool(
        "list_tags",
        (
            "Return the top-N most-used tags in the user's notes. Use this "
            "when the user is adding a note but hasn't specified a tag — "
            "suggest the top 4 so they can reuse one, or pick 'skip'."
        ),
        ListTagsArgs,
    ),
    _tool(
        "search_notes",
        (
            "Semantic search over all notes by natural-language query. Matches "
            "by MEANING, not exact words (handles typos and synonyms). Returns "
            "up to `limit` notes ranked by similarity, filtered by a 0.5 "
            "threshold. Use this for ANY free-text note lookup, reference "
            "phrase ('the meeting note', 'my lunch note'), or when the user "
            "describes a note they're looking for."
        ),
        SearchNotesArgs,
    ),
    _tool(
        "get_note",
        (
            "Fetch ONE note's full details by integer id. The id MUST come from "
            "a prior tool result (search_notes, list_notes, list_tags, add_note). "
            "NEVER invent an id."
        ),
        GetNoteArgs,
    ),
    _tool(
        "update_note",
        (
            "Patch an EXISTING note after the user has confirmed the proposed "
            "changes. `note_id` MUST come from a prior tool result — NEVER "
            "invent one. Only call after showing the user what the new fields "
            "will be and receiving affirmative confirmation. Pass `clear_tag=true` "
            "to remove a tag (distinct from omitting tag which leaves it alone)."
        ),
        UpdateNoteArgs,
    ),
    _tool(
        "delete_note",
        (
            "Delete a note by id. DESTRUCTIVE — two-step: first call with "
            "`confirm=false` to get a preview, then ask the user plainly "
            "('Delete note #N — [title]?'), then call again with "
            "`confirm=true` after they say yes. The `note_id` MUST come from "
            "a prior tool result."
        ),
        DeleteNoteArgs,
    ),
]


TOOL_NAMES: set[str] = {t["function"]["name"] for t in TOOL_DEFS}


ARG_MODELS: dict[str, type[BaseModel]] = {
    "add_note": AddNoteArgs,
    "list_notes": ListNotesArgs,
    "list_tags": ListTagsArgs,
    "search_notes": SearchNotesArgs,
    "get_note": GetNoteArgs,
    "update_note": UpdateNoteArgs,
    "delete_note": DeleteNoteArgs,
}
