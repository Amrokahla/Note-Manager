---
name: 02-data-models
description: Data models used by the Note Agent — Pydantic domain DTOs, the normalized LLM shape, provider-specific mappers for Ollama and Gemini, the SQLite single-table schema, embedding blob layout, and the tool-argument / tool-result envelope.
---

# 02 - Data Models

This document catalogues every data shape in the system and how they are translated across boundaries. There are three boundaries that matter:

1. **LLM ↔ orchestrator** — a normalized shape the orchestrator sees, with provider-specific mappers on each side.
2. **Orchestrator ↔ tools** — validated Pydantic argument models in, a uniform `ToolResult` envelope out.
3. **Services ↔ SQLite** — raw `sqlite3.Row` in, Pydantic domain models out.

The codebase uses **Pydantic v2** for anything that crosses an external boundary (tool arguments, HTTP DTOs, tool results) and **plain dataclasses** for internal state (`SessionState`, `Settings`).

## Map of Every Model

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          EXTERNAL / BOUNDARY                                 │
│                                                                              │
│  HTTP DTOs (backend/main.py)                                                 │
│     ChatIn          { session_id, message, model }                           │
│     ChatOut         { session_id, reply, tool_calls[] }                      │
│     ChatToolCall    { id, name, arguments, result }                          │
│                                                                              │
│  Tool arguments (backend/tools/schemas.py)                                   │
│     AddNoteArgs     { title, description, tag?, confirm }                    │
│     ListNotesArgs   { tag?, limit }                                          │
│     ListTagsArgs    { limit }                                                │
│     SearchNotesArgs { query, limit }                                         │
│     GetNoteArgs     { note_id }                                              │
│     UpdateNoteArgs  { note_id, title?, description?, tag?, clear_tag, confirm}│
│     DeleteNoteArgs  { note_id, confirm }                                     │
│                                                                              │
│  Tool result envelope (backend/tools/schemas.py)                             │
│     ToolResult      { ok, message, data?, needs_confirmation,                │
│                        candidates?, error_code? }                            │
│                                                                              │
│  LLM normalized shape (backend/agent/llm_types.py)                           │
│     ToolCall        { name, arguments }                                      │
│     LLMResponse     { kind: "tool_calls" | "message", content?, tool_calls, raw?}│
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                               INTERNAL                                       │
│                                                                              │
│  Domain DTOs (backend/services/models.py)                                    │
│     Note            { id, title, description, tag?, created_at, updated_at } │
│     NoteSummary     { id, title, description, tag?, updated_at, similarity?}│
│     TagCount        { tag, count }                                           │
│                                                                              │
│  Session state (backend/agent/conversation_state.py)                         │
│     SessionState    { session_id, messages(deque), last_referenced_note_ids, │
│                        pending_confirmation? }                               │
│                                                                              │
│  Settings (backend/config.py)                                                │
│     Settings        frozen dataclass — every env-configurable knob            │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Why Pydantic v2

Tool arguments come from an LLM — a source that can and will produce malformed input. Every field must be validated before the service layer sees it. Pydantic gives three things we rely on:

1. **Validation at the boundary** — `ArgsModel.model_validate(raw)` raises `ValidationError` on bad input; `note_tools.execute()` catches it and returns `ToolResult(ok=False, error_code="invalid_arg", ...)` so the LLM gets a clean message instead of a stack trace.
2. **JSON Schema generation** — `model_json_schema()` is what we hand to the LLM as the tool's parameter schema. The schema is never hand-written, so renaming a field or tightening a constraint never drifts between Python and the model.
3. **Round-trip serialization** — `result.model_dump(mode="json")` is how a `ToolResult` becomes the payload of an SSE event; `result.model_dump_json()` is how a tool response becomes a message in `state.messages`.

Internal-only shapes (session state, settings) don't need validation — they're built by code we control. Dataclasses are enough.

## Tool Argument Models

Every tool has exactly one Pydantic argument model. Field constraints are the enforcement point — a `title` longer than 200 characters never makes it past `model_validate`.

```python
# backend/tools/schemas.py

class AddNoteArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    tag: str | None = Field(default=None, max_length=50)
    confirm: bool = Field(default=False, description=...)


class SearchNotesArgs(BaseModel):
    query: str = Field(..., min_length=1, description="Natural-language query for semantic search.")
    limit: int = Field(default=5, ge=1, le=20)


class UpdateNoteArgs(BaseModel):
    note_id: int = Field(..., ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1)
    tag: str | None = Field(default=None, max_length=50)
    clear_tag: bool = Field(default=False, description=...)
    confirm: bool = Field(default=False, description=...)


class DeleteNoteArgs(BaseModel):
    note_id: int = Field(..., ge=1)
    confirm: bool = Field(default=False, description=...)
```

**`confirm` is the two-step gate**. It is a first-class field on every destructive or creative tool. The LLM must pass `confirm=false` first (the server returns a preview), then `confirm=true` on a second call after the user has explicitly agreed. See [03 - Note Agent](./03-note-agent.md) for the flow.

**`clear_tag` disambiguates "don't touch this field" from "remove the value"** in `UpdateNoteArgs`. Passing `tag=None` means "leave the current tag alone"; passing `clear_tag=True` means "set it to NULL". Without the boolean, `None` would be ambiguous.

### Small-Model Robustness: `_coerce_json_list`

Some Ollama-hosted models emit list arguments as a JSON-encoded string (`'["work"]'` instead of `["work"]`). A single `BeforeValidator` on the aliased `StrList` type parses those once at validation time so the rest of the codebase stays strictly typed:

```python
def _coerce_json_list(v: Any) -> Any:
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return v
        if isinstance(parsed, list):
            return parsed
    return v

StrList = Annotated[list[str], BeforeValidator(_coerce_json_list)]
```

## The `ToolResult` Envelope

Every tool — without exception — returns this shape. The LLM never sees a bare dict, a partial result, or a raised exception.

```python
ErrorCode = Literal["not_found", "invalid_arg", "ambiguous", "needs_confirmation", "internal"]

class ToolResult(BaseModel):
    ok: bool
    message: str
    data: Any | None = None
    needs_confirmation: bool = False
    candidates: list[dict] | None = None
    error_code: ErrorCode | None = None
```

| Field                | When populated                                                 |
|----------------------|----------------------------------------------------------------|
| `ok`                 | `true` on success or an expected "soft" outcome; `false` on validation errors, not-found, or internal failures. |
| `message`            | Human-readable string the LLM echoes / paraphrases to the user. |
| `data`               | Domain payload — a note, a list of notes, a preview. `None` when nothing to return. |
| `needs_confirmation` | `true` when a `confirm=false` preview was generated; pairs with `error_code="needs_confirmation"`. |
| `candidates`         | List of disambiguation options when a search returns >1 strong match. |
| `error_code`         | Programmatic tag the prompt can switch on (`not_found`, `invalid_arg`, etc.). |

### Why an Envelope Instead of Raising

A raised Python exception would have to be caught at the boundary, serialized into a string, and handed to the LLM anyway — and nothing stops different tools from producing different string formats. The envelope makes the contract explicit: **every tool, every outcome, one shape**. The system prompt then has a small, finite set of `error_code` values to reason about.

### Needs-Confirmation is an "`ok: false`"

A preview is not a successful write — the note hasn't been persisted. The envelope carries `ok=false, needs_confirmation=true, error_code="needs_confirmation"`. This prevents the model from ever reading a preview as a commit. See [03 - Note Agent](./03-note-agent.md).

## `TOOL_DEFS` — The Handoff to the Model

```python
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
    _tool("add_note",    "Save a NEW note after the user has explicitly confirmed...", AddNoteArgs),
    _tool("list_notes",  "List recent notes, optionally filtered by tag...",           ListNotesArgs),
    _tool("list_tags",   "Return the top-N most-used tags...",                         ListTagsArgs),
    _tool("search_notes","Semantic search over all notes...",                          SearchNotesArgs),
    _tool("get_note",    "Fetch ONE note's full details by integer id...",             GetNoteArgs),
    _tool("update_note", "Patch an EXISTING note after the user has confirmed...",     UpdateNoteArgs),
    _tool("delete_note", "Delete a note by id. DESTRUCTIVE — two-step...",             DeleteNoteArgs),
]

TOOL_NAMES: set[str] = {t["function"]["name"] for t in TOOL_DEFS}
```

This is the OpenAI-style "function calling" format. Ollama speaks it natively. Gemini uses a different shape — `llm_gemini.py` translates `TOOL_DEFS` into `genai_types.FunctionDeclaration` at every call. The descriptions are tuned for the LLM and cover preconditions ("only call after the user has confirmed"); see [03 - Note Agent](./03-note-agent.md) for the per-tool semantics.

## Normalized LLM Shape

```python
# backend/agent/llm_types.py

class ToolCall(BaseModel):
    name: str
    arguments: dict

class LLMResponse(BaseModel):
    kind: Literal["tool_calls", "message"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: dict | None = None
```

Both providers return this shape. The orchestrator switches on `resp.kind`:

- `kind == "message"` → emit `assistant` event, end turn.
- `kind == "tool_calls"` → run each call, loop back for the next hop.

The canonical message format accepted by `llm_*.chat(messages, ...)` is a list of dicts mirroring the OpenAI / Ollama convention:

```python
{"role": "system",    "content": "..."}
{"role": "user",      "content": "..."}
{"role": "assistant", "content": "..."}
{"role": "assistant", "tool_calls": [{"function": {"name": "...", "arguments": {...}}}]}
{"role": "tool",      "name": "...", "content": "<JSON of ToolResult>"}
```

## Provider Mapper — Ollama

Ollama natively speaks our format, so the mapper is thin: pass `messages` and `TOOL_DEFS` through, then normalize the response.

### Request

```python
client.chat(
    model=target_model,
    messages=messages,                 # already in canonical shape
    tools=tools if tools is not None else TOOL_DEFS,
    options={"temperature": 0.2},
    stream=stream,
)
```

Temperature is pinned at `0.2` — low enough to prefer tool calls over creative prose, not zero so the model doesn't loop on refusals.

### Response Normalization

```python
def _normalize_response(data: dict) -> LLMResponse:
    msg = data.get("message") or {}
    raw_tool_calls = msg.get("tool_calls") or []
    if raw_tool_calls:
        calls = [ToolCall(name=fn["name"], arguments=_coerce_arguments(fn["arguments"]))
                 for tc in raw_tool_calls for fn in [tc.get("function") or {}]]
        return LLMResponse(kind="tool_calls", tool_calls=calls, raw=data)

    text = msg.get("content") or ""
    # Small-model salvage: some llama variants emit a tool call as JSON in `content`
    maybe = _try_parse_toolcall_from_text(text)
    if maybe is not None:
        return LLMResponse(kind="tool_calls", tool_calls=[maybe], raw=data)

    return LLMResponse(kind="message", content=text, raw=data)
```

Two small-model repairs live here and nowhere else:

1. **`_coerce_arguments`** — some Ollama versions stringify `arguments` as JSON. Parse once into a dict.
2. **`_try_parse_toolcall_from_text`** — some 3B models emit the whole tool call as a JSON object inside `content` instead of populating `tool_calls`. Regex-match the first `{...}` block, parse, and if `name` is one of `TOOL_NAMES`, synthesize a `ToolCall`.

### Streaming

Streaming is enabled when `on_delta` is passed. The SDK yields chunk-shaped dicts; the provider accumulates `content` deltas (forwarded to `on_delta`) and `tool_calls`, then synthesizes a single response dict at the end and runs it through `_normalize_response`.

## Provider Mapper — Gemini

Gemini's API uses a different shape for messages, a different shape for tool declarations, and JSON Schema with restrictions. All three translations live in `backend/agent/llm_gemini.py`.

### Message Translation

| Our canonical shape                                                         | Gemini shape                                                           |
|-----------------------------------------------------------------------------|------------------------------------------------------------------------|
| `{"role": "system", "content": "..."}`                                      | concatenated into `GenerateContentConfig.system_instruction`           |
| `{"role": "user", "content": "..."}`                                        | `{"role": "user", "parts": [{"text": "..."}]}`                         |
| `{"role": "assistant", "content": "..."}`                                   | `{"role": "model", "parts": [{"text": "..."}]}`                        |
| `{"role": "assistant", "tool_calls": [{"function": {"name", "arguments"}}]}`| `{"role": "model", "parts": [{"function_call": {"name", "args"}}]}`    |
| `{"role": "tool", "name", "content": "<JSON>"}`                             | `{"role": "user", "parts": [{"function_response": {"name", "response": <parsed>}}]}` |

All system messages (the base system prompt + the per-turn `(context)` line) are concatenated with blank lines and passed once via `system_instruction`.

### Repairing Broken Function-Call Pairs

Gemini enforces that every `function_call` turn is immediately followed by a `function_response` turn. History from an earlier crashed turn can violate this and cause `400 INVALID_ARGUMENT`. `_repair_function_pairs()` walks the translated `contents` list and drops any orphan call or response so the sequence sent to Gemini is always well-formed.

```python
def _repair_function_pairs(contents: list[dict]) -> list[dict]:
    """Drop orphan function_call / function_response turns so Gemini's pairing rule holds."""
```

### Tool Schema Translation

Gemini's tool declarations are JSON Schema with restrictions:

- No `$ref` / `$defs` — references must be inlined.
- No `anyOf: [X, {"type": "null"}]` — nullable fields use `nullable: true` directly on the non-null branch.
- No `$schema` or metadata keys at the schema-node level.
- `title` is rejected **as schema metadata** but is a valid **property name** (our `AddNoteArgs` has a `title` property!).

`_normalize_schema_for_gemini()` walks the Pydantic-generated schema and rewrites it:

```python
_UNSUPPORTED_AT_NODE = {"$schema"}

def _normalize_schema_for_gemini(schema: dict) -> dict:
    """Inline $refs, collapse anyOf[X, null] to nullable, drop keys Gemini rejects."""
    # walks the schema recursively:
    #   - $ref → inline the referenced $defs entry
    #   - anyOf[X, {"type": "null"}] → {X, "nullable": true}
    #   - {"title": "..."} at a schema node → dropped
    #   - inside "properties", property names (which may be literally "title") are preserved
```

The `title` rule is the subtle part: drop it when it's a **sibling** of `type` / `properties`, keep it when it's a **key inside** `properties`.

### Safety Filters

Gemini's safety filters are off for this app — note text is user content ("delete everything", "dark thoughts") and the platform has no moderation requirement:

```python
def _safety_block_none() -> list[genai_types.SafetySetting]:
    categories = [
        "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT",
    ]
    return [genai_types.SafetySetting(category=c, threshold="BLOCK_NONE") for c in categories]
```

### Error Mapping

Raw Gemini SDK errors are large JSON blobs that leak directly into the UI unless translated. `_cleanup_gemini_error()` maps well-known HTTP statuses to short user-facing messages:

- `429` → rate limit hit; try Flash or Ollama.
- `403` → API key lacks access to the requested model.
- `400` → first line of the error passed through as-is.
- `ServerError` → "Gemini is having problems, try again".

### Response Normalization

Gemini returns `candidates[].content.parts[]`. Each part is either a `text` chunk or a `function_call`. The normalizer walks every part, collects tool calls into `ToolCall(name, arguments)`, and if any were collected returns `kind="tool_calls"` — else `kind="message"` with concatenated text.

Streaming iterates `candidates[].content.parts` directly (rather than `chunk.text`) to avoid SDK warnings about mixed text + function_call chunks.

## Domain Models

```python
# backend/services/models.py

class Note(BaseModel):
    id: int
    title: str
    description: str
    tag: str | None = None
    created_at: datetime
    updated_at: datetime


class NoteSummary(BaseModel):
    """Compact form for list and search results."""
    id: int
    title: str
    description: str
    tag: str | None = None
    updated_at: datetime
    similarity: float | None = None   # populated by semantic search only


class TagCount(BaseModel):
    tag: str
    count: int
```

`Note` is the full-fat shape returned by `get_note`, `create_note`, `update_note`. `NoteSummary` is for list / search responses — omits `created_at` and carries an optional `similarity` score. `TagCount` is the shape `list_tags` returns. All three are Pydantic so `.model_dump(mode="json")` produces clean JSON for the `ToolResult.data` payload.

## SQLite — Single Table, No ORM

The schema is one table plus two indices. There is no ORM. Every query is raw SQL you can read and defend in an interview.

```sql
CREATE TABLE IF NOT EXISTS notes (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  title                 TEXT    NOT NULL,
  description           TEXT    NOT NULL,
  tag                   TEXT,
  embedding             BLOB,
  embedding_updated_at  TEXT,
  created_at            TEXT    NOT NULL,
  updated_at            TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_tag
  ON notes(tag) WHERE tag IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_notes_updated_at
  ON notes(updated_at DESC);
```

### Design Decisions

| Choice                                       | Reason                                                                 |
|----------------------------------------------|------------------------------------------------------------------------|
| **Single table, denormalised tag**           | One tag per note — no join, no `note_tags` table. Simpler queries, trivial index on `tag`. |
| **Embedding in-row as BLOB**                 | 768 × 4 = 3072 bytes per note. Keeping it in `notes` removes a join and a second table. |
| **`embedding` nullable**                     | `NULL` means "not yet embedded" — the startup backfill has a trivial `WHERE embedding IS NULL`. |
| **No FTS5**                                  | Semantic search via `nomic-embed-text` supersedes keyword matching; tag queries are plain `WHERE`. |
| **ISO strings for timestamps**               | `updated_at DESC` sorts lexicographically on ISO-8601 strings. No date parsing in queries. |
| **Partial index on `tag`**                   | `WHERE tag IS NOT NULL` keeps the index small when most notes are untagged. |
| **No ORM**                                   | Raw SQL is readable at a glance. The project invariant: every query must be defensible. |

### Connection Lifecycle — `tx()`

All SQL goes through the context manager. Transactions are automatic: commit on success, rollback on exception, always close.

```python
@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

`_connect()` creates the parent `DB_PATH` directory if missing, sets `row_factory = sqlite3.Row` (so services can read columns by name: `row["title"]`), and enables `PRAGMA foreign_keys = ON` even though the current schema has no foreign keys — the pragma is cheap and future-proof.

### Embedding BLOB Layout

Embeddings are serialized as little-endian packed `float32`:

```python
def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()

def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
```

`nomic-embed-text` produces 768-dim vectors. They are **normalized to unit length at write time** so cosine similarity at query time collapses to a dot product — a hot-loop optimisation the `cosine()` function uses when both norms are within `1e-5` of `1.0`.

### Tag Normalization

Tags are normalized on every write path so queries can stay a plain `WHERE tag = ?`. The normalization rules are strip → strip leading `#` → lowercase → `None` if empty.

```python
def normalize_tag(t: str | None) -> str | None:
    if t is None:
        return None
    cleaned = t.strip().lstrip("#").lower()
    return cleaned or None
```

`"#Work"`, `"  work "`, and `"WORK"` all store as `"work"` and match each other at query time.

### Row → Domain Translation

Two small helpers turn `sqlite3.Row` into Pydantic. The service never passes a raw row up.

```python
def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"], title=row["title"], description=row["description"],
        tag=row["tag"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )

def _row_to_summary(row: sqlite3.Row, similarity: float | None = None) -> NoteSummary:
    return NoteSummary(
        id=row["id"], title=row["title"], description=row["description"],
        tag=row["tag"], updated_at=row["updated_at"],
        similarity=similarity,
    )
```

### Why Pydantic for DTOs but Not Rows

Pydantic validates that a row coming out of SQLite has the types the rest of the app expects, costs almost nothing on read, and gives us `.model_dump(mode="json")` for free when the DTO becomes a `ToolResult.data` payload or an HTTP response body.

The **row itself** stays `sqlite3.Row` inside the service — it's ephemeral, typed-ish (columns by name via `row_factory`), and never crosses a boundary.

## HTTP DTOs

```python
# backend/main.py

class ChatIn(BaseModel):
    session_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    model: str = Field(default=DEFAULT_MODEL)

class ChatToolCall(BaseModel):
    id: str
    name: str
    arguments: dict
    result: dict

class ChatOut(BaseModel):
    session_id: str
    reply: str
    tool_calls: list[ChatToolCall]
```

`ChatToolCall.result` is `dict` — not `ToolResult` — because by the time it hits the response it has already been `.model_dump(mode="json")`-ed to keep JSON-native types (`datetime` → ISO string).

`_resolved_model()` silently coerces unknown `model` ids to `DEFAULT_MODEL`; this keeps older frontend builds working after a provider is renamed.

## Session State Models

```python
# backend/agent/conversation_state.py

@dataclass
class SessionState:
    session_id: str
    messages: deque[dict] = field(default_factory=lambda: deque(maxlen=_MAX_MESSAGES))
    last_referenced_note_ids: list[int] = field(default_factory=list)
    pending_confirmation: dict | None = None
```

- `messages` is a rolling window capped at `settings.history_turns * 2` entries — see [04 - Memory and State](./04-memory-and-state.md).
- `last_referenced_note_ids` is the deduplicated list of ids from the most recent tool result.
- `pending_confirmation` is either `None` or `{"tool": str, "args": dict}` — the merged arguments of a tool that returned `needs_confirmation: true`.

Plain dataclasses, not Pydantic — nothing external writes to them.

## Frontend Type Mirrors

TypeScript types are hand-mirrored to Python shapes in `app/src/app/types.ts` — there is no code generator (no tygo, no protoc) in this project. The contract between backend and frontend is small enough to maintain manually, and the test suite would catch any drift.

```typescript
// app/src/app/types.ts

export type ToolStatus = "running" | "ok" | "fail" | "needs_confirmation";

export interface ToolCallRecord {
  id: string;
  turnId: string;
  name: string;
  arguments: Record<string, unknown>;
  status: ToolStatus;
  message?: string;
  errorCode?: string;
  durationMs?: number;
  startedAt: number;
  endedAt?: number;
}
```

`ToolCallRecord` is the frontend-only shape held in the reducer. The server-sent `tool_call` and `tool_result` events are merged client-side (see `app/src/app/lib/reducer.ts`) — `startedAt` and `durationMs` live only in the browser.

## Related Docs

- [01 - Architecture](./01-architecture.md) — the high-level picture and file map.
- [03 - Note Agent](./03-note-agent.md) — how tool arguments and results flow through the agent loop.
- [04 - Memory and State](./04-memory-and-state.md) — session state internals and context line injection.
