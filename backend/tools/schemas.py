from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# --- Tool argument models -----------------------------------------------------
#
# One model per user-facing intent. Each model is the single source of truth
# for what the LLM is allowed to send us for that tool.

class AddNoteArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list, max_length=20)


class SearchNotesArgs(BaseModel):
    query: str | None = Field(
        default=None,
        description="Keyword or natural-language query. Matched against title + body via FTS5.",
    )
    tags: list[str] = Field(default_factory=list, max_length=20)
    date_from: datetime | None = Field(
        default=None,
        description="Only return notes created at or after this ISO-8601 timestamp.",
    )
    date_to: datetime | None = Field(
        default=None,
        description="Only return notes created at or before this ISO-8601 timestamp.",
    )
    limit: int = Field(default=10, ge=1, le=50)
    semantic: bool = Field(
        default=False,
        description="Use vector similarity instead of keyword search (bonus feature).",
    )


class GetNoteArgs(BaseModel):
    note_id: int = Field(..., ge=1)


class UpdateNoteArgs(BaseModel):
    note_id: int = Field(..., ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1)
    tags: list[str] | None = Field(default=None, max_length=20)


class DeleteNoteArgs(BaseModel):
    note_id: int = Field(..., ge=1)
    confirm: bool = Field(
        default=False,
        description=(
            "Must be true for the deletion to proceed. First call with confirm=false "
            "to preview; only pass confirm=true after the user has explicitly agreed."
        ),
    )


class ListRecentArgs(BaseModel):
    limit: int = Field(default=5, ge=1, le=50)


class SummarizeNotesArgs(BaseModel):
    note_ids: list[int] = Field(..., min_length=1, max_length=20)


# --- Uniform tool result envelope --------------------------------------------
#
# Every tool call the LLM makes comes back as one of these. Keeping the shape
# uniform lets the system prompt teach the model a single pattern:
#   if ok:       use data
#   elif needs_confirmation: ask the user
#   elif candidates:         disambiguate with the user
#   else:                    explain the error in plain English.

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


# --- Ollama tool descriptors -------------------------------------------------
#
# Ollama accepts OpenAI-style function specs. We derive them from the Pydantic
# models so the JSON schema stays in lockstep with the Python types. The
# descriptions are LLM-facing prompt surface — keep them tight and action-oriented.

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
        "Create a new note with a title, body, and optional tags.",
        AddNoteArgs,
    ),
    _tool(
        "search_notes",
        (
            "Search notes by keyword, tags, and/or date range. "
            "Returns compact summaries; call get_note for full contents."
        ),
        SearchNotesArgs,
    ),
    _tool(
        "get_note",
        "Fetch a single note's full contents by its id.",
        GetNoteArgs,
    ),
    _tool(
        "update_note",
        (
            "Patch a note's title, body, and/or tags. Only provide the fields you "
            "want to change; omitted fields are left untouched. Passing tags "
            "replaces the whole tag set."
        ),
        UpdateNoteArgs,
    ),
    _tool(
        "delete_note",
        (
            "Delete a note by id. First call with confirm=false to get a preview and "
            "a needs_confirmation response; only call again with confirm=true after "
            "the user has explicitly agreed."
        ),
        DeleteNoteArgs,
    ),
    _tool(
        "list_recent",
        "List the N most recently updated notes as summaries.",
        ListRecentArgs,
    ),
    _tool(
        "summarize_notes",
        (
            "Fetch several notes by id so the assistant can reason over their "
            "contents (e.g. summarise, compare, find contradictions)."
        ),
        SummarizeNotesArgs,
    ),
]


TOOL_NAMES: set[str] = {t["function"]["name"] for t in TOOL_DEFS}


ARG_MODELS: dict[str, type[BaseModel]] = {
    "add_note": AddNoteArgs,
    "search_notes": SearchNotesArgs,
    "get_note": GetNoteArgs,
    "update_note": UpdateNoteArgs,
    "delete_note": DeleteNoteArgs,
    "list_recent": ListRecentArgs,
    "summarize_notes": SummarizeNotesArgs,
}
