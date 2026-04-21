from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.tools.schemas import (
    ARG_MODELS,
    AddNoteArgs,
    DeleteNoteArgs,
    GetNoteArgs,
    ListRecentArgs,
    SearchNotesArgs,
    SummarizeNotesArgs,
    TOOL_DEFS,
    TOOL_NAMES,
    ToolResult,
    UpdateNoteArgs,
)


# ---------- Happy-path round-trips -------------------------------------------

def test_add_note_roundtrip():
    payload = {"title": "hello", "body": "world", "tags": ["a", "b"]}
    m = AddNoteArgs.model_validate(payload)
    assert m.title == "hello"
    assert m.tags == ["a", "b"]
    # default for tags when absent
    m2 = AddNoteArgs.model_validate({"title": "t", "body": "b"})
    assert m2.tags == []


def test_search_notes_roundtrip_all_optional():
    m = SearchNotesArgs.model_validate({})
    assert m.query is None
    assert m.tags == []
    assert m.limit == 10
    assert m.semantic is False

    m2 = SearchNotesArgs.model_validate(
        {
            "query": "deadline",
            "tags": ["work"],
            "date_from": "2026-04-01T00:00:00",
            "date_to": "2026-04-30T23:59:59",
            "limit": 20,
            "semantic": True,
        }
    )
    assert m2.query == "deadline"
    assert m2.limit == 20


def test_get_note_roundtrip():
    m = GetNoteArgs.model_validate({"note_id": 42})
    assert m.note_id == 42


def test_update_note_partial_patch():
    m = UpdateNoteArgs.model_validate({"note_id": 1})
    assert m.title is None and m.body is None and m.tags is None

    m2 = UpdateNoteArgs.model_validate({"note_id": 1, "tags": ["x"]})
    assert m2.tags == ["x"]


def test_delete_note_defaults_confirm_false():
    m = DeleteNoteArgs.model_validate({"note_id": 7})
    assert m.confirm is False

    m2 = DeleteNoteArgs.model_validate({"note_id": 7, "confirm": True})
    assert m2.confirm is True


def test_list_recent_defaults_and_bounds():
    assert ListRecentArgs.model_validate({}).limit == 5
    assert ListRecentArgs.model_validate({"limit": 50}).limit == 50


def test_summarize_notes_requires_ids():
    m = SummarizeNotesArgs.model_validate({"note_ids": [1, 2, 3]})
    assert m.note_ids == [1, 2, 3]


# ---------- Malformed payload rejection --------------------------------------

def test_add_note_rejects_empty_title():
    with pytest.raises(ValidationError):
        AddNoteArgs.model_validate({"title": "", "body": "b"})


def test_add_note_rejects_missing_body():
    with pytest.raises(ValidationError):
        AddNoteArgs.model_validate({"title": "t"})


def test_search_notes_rejects_limit_out_of_range():
    with pytest.raises(ValidationError):
        SearchNotesArgs.model_validate({"limit": 0})
    with pytest.raises(ValidationError):
        SearchNotesArgs.model_validate({"limit": 100})


def test_search_notes_rejects_bad_datetime():
    with pytest.raises(ValidationError):
        SearchNotesArgs.model_validate({"date_from": "not-a-date"})


def test_get_note_rejects_non_positive_id():
    with pytest.raises(ValidationError):
        GetNoteArgs.model_validate({"note_id": 0})
    with pytest.raises(ValidationError):
        GetNoteArgs.model_validate({"note_id": -5})


def test_update_note_rejects_missing_id():
    with pytest.raises(ValidationError):
        UpdateNoteArgs.model_validate({"title": "x"})


def test_delete_note_rejects_missing_id():
    with pytest.raises(ValidationError):
        DeleteNoteArgs.model_validate({"confirm": True})


def test_list_recent_rejects_out_of_range():
    with pytest.raises(ValidationError):
        ListRecentArgs.model_validate({"limit": 0})
    with pytest.raises(ValidationError):
        ListRecentArgs.model_validate({"limit": 9999})


def test_summarize_notes_rejects_empty_list():
    with pytest.raises(ValidationError):
        SummarizeNotesArgs.model_validate({"note_ids": []})


# ---------- ToolResult envelope ---------------------------------------------

def test_tool_result_happy_path():
    r = ToolResult(ok=True, message="done", data={"id": 1})
    assert r.ok is True
    assert r.needs_confirmation is False
    assert r.error_code is None


def test_tool_result_needs_confirmation():
    r = ToolResult(
        ok=False,
        message="confirm?",
        needs_confirmation=True,
        error_code="needs_confirmation",
        data={"preview": {"id": 1}},
    )
    assert r.error_code == "needs_confirmation"


def test_tool_result_rejects_bad_error_code():
    with pytest.raises(ValidationError):
        ToolResult(ok=False, message="x", error_code="bogus")  # type: ignore[arg-type]


# ---------- TOOL_DEFS shape (what Ollama will see) --------------------------

def test_tool_defs_cover_every_arg_model():
    assert TOOL_NAMES == set(ARG_MODELS.keys())
    assert len(TOOL_DEFS) == len(ARG_MODELS)


def test_tool_defs_are_openai_function_shape():
    for entry in TOOL_DEFS:
        assert entry["type"] == "function"
        fn = entry["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        params = fn["parameters"]
        # Pydantic v2 emits a JSON schema with type=object at the root.
        assert params["type"] == "object"
        assert "properties" in params


@pytest.mark.parametrize("tool_name,model", list(ARG_MODELS.items()))
def test_tool_def_schema_matches_model_schema(tool_name, model):
    entry = next(t for t in TOOL_DEFS if t["function"]["name"] == tool_name)
    assert entry["function"]["parameters"] == model.model_json_schema()
