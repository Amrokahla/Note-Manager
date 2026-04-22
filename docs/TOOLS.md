# Tool Reference

Every tool the LLM can call, with its arguments, return envelope, error codes, and a representative call / response pair. The canonical definitions live in `backend/tools/schemas.py` (Pydantic models + `TOOL_DEFS`); this document is the readable mirror — if something drifts, `schemas.py` wins.

## The Uniform Result Envelope

Every tool returns a `ToolResult`:

| Field | Type | When populated |
|---|---|---|
| `ok` | `bool` | `true` on success or a soft outcome (e.g. empty search). `false` on validation errors, not-found, internal failures, and preview responses. |
| `message` | `str` | Human-readable string the model echoes / paraphrases to the user. |
| `data` | `Any \| null` | Domain payload — a `Note`, a list of `NoteSummary`, a preview wrapper. `null` when there's nothing to return. |
| `needs_confirmation` | `bool` | `true` when a `confirm=false` preview was generated. Always paired with `ok=false` and `error_code="needs_confirmation"`. |
| `candidates` | `list[dict] \| null` | Disambiguation options returned by `search_notes` when >1 strong match. |
| `error_code` | `ErrorCode \| null` | One of `not_found`, `invalid_arg`, `ambiguous`, `needs_confirmation`, `internal`. |

---

## `add_note`

Create a new note. **Two-step.** First call with `confirm=false` to receive a preview; second call with `confirm=true` after the user has explicitly agreed.

### Arguments

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `title` | `str` | ✓ | 1–200 chars | Short label. |
| `description` | `str` | ✓ | ≥ 1 char | Body / details. |
| `tag` | `str \| null` | — | ≤ 50 chars | Omit rather than guess. Normalised on write (strip `#`, lowercase). |
| `confirm` | `bool` | — | default `false` | Must be `true` for the save to commit. |

### Preview response (first call)

```json
{
  "ok": false,
  "needs_confirmation": true,
  "error_code": "needs_confirmation",
  "message": "About to save this note — title '...', tag 'meeting'. Show this preview to the user and ask them to confirm before calling add_note again with confirm=true.",
  "data": { "preview": { "title": "...", "description": "...", "tag": "meeting" } }
}
```

### Commit response (second call)

```json
{
  "ok": true,
  "message": "Created note #42 'Meeting on Wednesday'.",
  "data": {
    "id": 42,
    "title": "Meeting on Wednesday",
    "description": "Sync with the finance team",
    "tag": "meeting",
    "created_at": "2026-04-22T14:30:00+00:00",
    "updated_at": "2026-04-22T14:30:00+00:00"
  }
}
```

### Error codes

- `invalid_arg` — Pydantic validation failed (empty title, too long, missing description, etc.).
- `needs_confirmation` — returned on every `confirm=false` call. Not really an error, but `ok: false` keeps the LLM from treating a preview as a commit.

---

## `list_notes`

List recent notes, optionally filtered by tag and/or a creation date range. Used for "show my notes", "list work notes", "what did I write last week".

### Arguments

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `tag` | `str \| null` | — | — | Normalised to lowercase. |
| `limit` | `int` | — | 1–50, default `10` | Row cap. |
| `date_from` | `datetime \| null` | — | ISO-8601 | Inclusive lower bound on `created_at`. Compute relative phrases ("last week") from today's date in the `(context)` line. |
| `date_to` | `datetime \| null` | — | ISO-8601 | Inclusive upper bound. |

Filters compose — tag + date range is legal and builds a single `WHERE … AND …`.

### Response

```json
{
  "ok": true,
  "message": "Found 2 note(s) tagged 'meeting' and from 2026-04-08 to 2026-04-22.",
  "data": [
    { "id": 12, "title": "Finance sync", "description": "...", "tag": "meeting", "updated_at": "2026-04-21T...", "similarity": null },
    { "id": 9,  "title": "Design review", "description": "...", "tag": "meeting", "updated_at": "2026-04-15T...", "similarity": null }
  ]
}
```

`similarity` is always `null` for `list_notes` (populated only by `search_notes`).

### Error codes

- `invalid_arg` — Pydantic validation (`limit` out of range, etc.).

---

## `list_tags`

Return the top-N most-used tags by count. Used primarily during the add flow to suggest reusing an existing tag.

### Arguments

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `limit` | `int` | — | 1–20, default `4` | Top-N cap. |

### Response

```json
{
  "ok": true,
  "message": "Top 3 tag(s).",
  "data": [
    { "tag": "work", "count": 12 },
    { "tag": "personal", "count": 5 },
    { "tag": "ideas", "count": 3 }
  ]
}
```

---

## `search_notes`

Semantic search over all notes by natural-language query. Uses `nomic-embed-text` embeddings and cosine similarity against a threshold (`SEARCH_THRESHOLD`, default `0.35`).

### Arguments

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `query` | `str` | ✓ | ≥ 1 char | Free-form natural language. |
| `limit` | `int` | — | 1–20, default `5` | Max results. |

### Three possible outcomes

**Above-threshold, single match:**

```json
{ "ok": true, "message": "Found 1 matching note.", "data": [ { "id": 7, "...": "..." } ] }
```

**Above-threshold, multiple matches** — ambiguity; `candidates` surfaced so the model asks the user to pick:

```json
{
  "ok": true,
  "message": "Found 3 matching note(s) — ask the user which one.",
  "data": [ ... ],
  "candidates": [ ... ]
}
```

**Below-threshold fallback** — nothing cleared the bar; closest few notes returned as low-confidence:

```json
{
  "ok": true,
  "message": "No strong match (nothing above similarity threshold). Here are the closest 3 note(s) as a best-effort fallback — tell the user no exact match was found and show them as possibilities.",
  "data": [ ... ]
}
```

**Empty corpus:**

```json
{ "ok": true, "message": "No notes at all (or none have embeddings yet).", "data": [] }
```

### Error codes

- `invalid_arg` — empty query (caught at validation, also `ValueError` from `embeddings.embed` on whitespace-only input).

---

## `get_note`

Fetch one note's full details by id.

### Arguments

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `note_id` | `int` | ✓ | ≥ 1 | Must come from a prior tool result — the prompt forbids invention. |

### Response (success)

```json
{
  "ok": true,
  "message": "Fetched note #42.",
  "data": { "id": 42, "title": "...", "description": "...", "tag": "...", "created_at": "...", "updated_at": "..." }
}
```

### Error codes

- `not_found` — no row with that id.
- `invalid_arg` — negative / zero id.

---

## `update_note`

Patch an existing note. **Two-step.** Any of `title` / `description` / `tag` / `clear_tag` can be set. Omitted fields are left alone.

### Arguments

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `note_id` | `int` | ✓ | ≥ 1 | Must come from a prior tool result. |
| `title` | `str \| null` | — | 1–200 chars when set | Omit to leave unchanged. |
| `description` | `str \| null` | — | ≥ 1 char when set | Same. |
| `tag` | `str \| null` | — | ≤ 50 chars when set | Normalised on write. |
| `clear_tag` | `bool` | — | default `false` | Set `true` to remove the tag (distinct from omitting). |
| `confirm` | `bool` | — | default `false` | Must be `true` to commit. |

### Behaviour

- **Auto-sync across fields.** If the LLM updates only one of title/description and the edit is a **numeric** substitution (e.g. `"5 pm"` → `"7 pm"`) that also applies to the other field, the service propagates it so both stay consistent. Plain word swaps aren't propagated — too many false positives.
- **Re-embed on text change.** Any change to `title` or `description` re-runs `nomic-embed-text`. Tag-only edits skip the embedding work.

### Preview response (first call)

```json
{
  "ok": false,
  "needs_confirmation": true,
  "error_code": "needs_confirmation",
  "message": "About to update note #42. Show this preview to the user and ask them to confirm before calling update_note again with confirm=true.",
  "data": { "preview": { "id": 42, "title": "...", "description": "...", "tag": "..." } }
}
```

### Commit response (second call)

```json
{
  "ok": true,
  "message": "Updated note #42.",
  "data": { "id": 42, "title": "...", "description": "...", "tag": "...", "created_at": "...", "updated_at": "..." }
}
```

### Error codes

- `not_found` — no row with that id.
- `invalid_arg` — all-empty patch (no fields provided, no `clear_tag`).
- `needs_confirmation` — preview step.

---

## `delete_note`

Delete a note by id. **Two-step. Destructive.** Unlike add/update, the commit-intent gate does **not** force `confirm=true` for delete — the LLM must set it explicitly on the second call.

### Arguments

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `note_id` | `int` | ✓ | ≥ 1 | Must come from a prior tool result. |
| `confirm` | `bool` | — | default `false` | Must be `true` to actually delete. |

### Preview response (first call)

```json
{
  "ok": false,
  "needs_confirmation": true,
  "error_code": "needs_confirmation",
  "message": "About to permanently delete note #42 'Meeting on Wednesday'. Ask the user to confirm before calling delete_note again with confirm=true.",
  "data": { "preview": { "id": 42, "title": "...", "description": "...", "tag": "...", "created_at": "...", "updated_at": "..." } }
}
```

### Commit response (second call)

```json
{ "ok": true, "message": "Deleted note #42." }
```

### Error codes

- `not_found` — no row with that id.
- `needs_confirmation` — preview step.

---

## Cross-cutting Notes

**Empty-string hygiene.** The orchestrator strips empty-string / whitespace-only values before validation (`_sanitize_args` in `intent_parser.py`), so models emitting `title: ""` to mean "I didn't compute a value" don't trip Pydantic's `min_length=1`.

**Merge-with-pending.** On a modification turn during a pending add/update, the orchestrator merges the new arguments onto the stored pending args — empty values don't clobber. Only `add_note` and `update_note` are mergeable; delete isn't.

**Uniform never-raises contract.** `backend/tools/note_tools.py:execute(...)` catches every exception the handlers could throw and converts to `ToolResult(ok=false, error_code="internal", message=...)`. The LLM never sees a stack trace.

## Source of Truth

- Pydantic models + `TOOL_DEFS`: `backend/tools/schemas.py`
- Dispatcher: `backend/tools/note_tools.py`
- Service layer (SQL + embeddings): `backend/services/note_service.py`
- Deep dive: [`03-note-agent.md`](03-note-agent.md)
- Per-file walkthroughs: [`files/backend/tools/schemas.md`](files/backend/tools/schemas.md), [`files/backend/tools/note_tools.md`](files/backend/tools/note_tools.md)
