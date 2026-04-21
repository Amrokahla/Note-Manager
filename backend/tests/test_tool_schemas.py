from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.tools.schemas import (
    ARG_MODELS,
    AddNoteArgs,
    DeleteNoteArgs,
    GetNoteArgs,
    ListNotesArgs,
    ListTagsArgs,
    SearchNotesArgs,
    TOOL_DEFS,
    TOOL_NAMES,
    ToolResult,
    UpdateNoteArgs,
)


# ---------- Happy-path round-trips -------------------------------------------

def test_add_note_roundtrip():
    m = AddNoteArgs.model_validate(
        {"title": "hi", "description": "there", "tag": "work"}
    )
    assert m.title == "hi"
    assert m.tag == "work"


def test_add_note_tag_is_optional():
    m = AddNoteArgs.model_validate({"title": "hi", "description": "there"})
    assert m.tag is None


def test_list_notes_defaults():
    m = ListNotesArgs.model_validate({})
    assert m.tag is None
    assert m.limit == 10


def test_list_notes_with_tag():
    m = ListNotesArgs.model_validate({"tag": "work"})
    assert m.tag == "work"


def test_list_tags_defaults():
    assert ListTagsArgs.model_validate({}).limit == 4


def test_search_notes_requires_query():
    with pytest.raises(ValidationError):
        SearchNotesArgs.model_validate({})


def test_search_notes_defaults():
    m = SearchNotesArgs.model_validate({"query": "standup"})
    assert m.query == "standup"
    assert m.limit == 5


def test_get_note_roundtrip():
    assert GetNoteArgs.model_validate({"note_id": 42}).note_id == 42


def test_update_note_partial_patch():
    m = UpdateNoteArgs.model_validate({"note_id": 1})
    assert m.title is None and m.description is None and m.tag is None
    assert m.clear_tag is False


def test_update_note_clear_tag():
    m = UpdateNoteArgs.model_validate({"note_id": 1, "clear_tag": True})
    assert m.clear_tag is True


def test_delete_note_defaults_confirm_false():
    assert DeleteNoteArgs.model_validate({"note_id": 7}).confirm is False


# ---------- Malformed payload rejection --------------------------------------

def test_add_note_rejects_empty_title():
    with pytest.raises(ValidationError):
        AddNoteArgs.model_validate({"title": "", "description": "b"})


def test_add_note_rejects_missing_description():
    with pytest.raises(ValidationError):
        AddNoteArgs.model_validate({"title": "t"})


def test_list_notes_rejects_limit_out_of_range():
    with pytest.raises(ValidationError):
        ListNotesArgs.model_validate({"limit": 0})
    with pytest.raises(ValidationError):
        ListNotesArgs.model_validate({"limit": 100})


def test_search_notes_rejects_empty_query():
    with pytest.raises(ValidationError):
        SearchNotesArgs.model_validate({"query": ""})


def test_get_note_rejects_non_positive_id():
    with pytest.raises(ValidationError):
        GetNoteArgs.model_validate({"note_id": 0})


def test_delete_note_rejects_missing_id():
    with pytest.raises(ValidationError):
        DeleteNoteArgs.model_validate({"confirm": True})


# ---------- ToolResult envelope ---------------------------------------------

def test_tool_result_happy_path():
    r = ToolResult(ok=True, message="done", data={"id": 1})
    assert r.ok is True
    assert r.error_code is None


def test_tool_result_rejects_bad_error_code():
    with pytest.raises(ValidationError):
        ToolResult(ok=False, message="x", error_code="bogus")  # type: ignore[arg-type]


# ---------- TOOL_DEFS shape ------------------------------------------------

def test_tool_defs_cover_every_arg_model():
    assert TOOL_NAMES == set(ARG_MODELS.keys())
    assert len(TOOL_DEFS) == len(ARG_MODELS)
    # 7 tools total per the redesign
    assert len(TOOL_DEFS) == 7


def test_tool_names_match_redesign():
    assert TOOL_NAMES == {
        "add_note",
        "list_notes",
        "list_tags",
        "search_notes",
        "get_note",
        "update_note",
        "delete_note",
    }


def test_tool_defs_are_openai_function_shape():
    for entry in TOOL_DEFS:
        assert entry["type"] == "function"
        fn = entry["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        assert fn["parameters"]["type"] == "object"


@pytest.mark.parametrize("tool_name,model", list(ARG_MODELS.items()))
def test_tool_def_schema_matches_model_schema(tool_name, model):
    entry = next(t for t in TOOL_DEFS if t["function"]["name"] == tool_name)
    assert entry["function"]["parameters"] == model.model_json_schema()
