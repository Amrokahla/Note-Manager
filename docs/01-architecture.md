---
name: 01-architecture
description: High-level architecture of the Note Agent backend — layers, LLM provider abstraction, tool-calling loop, SQLite + embeddings data layer, and the SSE streaming pipeline.
---

# 01 - Architecture Overview

A detailed look at how the Note Agent backend is put together: the layers, where each responsibility lives, how a chat turn flows through the system end-to-end, and how streaming is bridged from a synchronous orchestrator to an HTTP SSE response.

The guiding principle is **layer discipline**: each layer knows only about the layer directly below it. The LLM never touches SQLite; the SQL layer never knows a language model exists; the HTTP layer never runs business logic.

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                               CLIENT LAYER                                    │
├──────────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────────────────────┐ │
│  │                           Next.js 16 App                                 │ │
│  │     Chat (70%)  │  Tool-call panel (30%)  │  Model selector (header)    │ │
│  │             React 19 + useReducer (no state library)                     │ │
│  └─────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
                                       │
                     POST /chat/stream  (Server-Sent Events)
                                       │
┌──────────────────────────────────────────────────────────────────────────────┐
│                               HTTP LAYER                                      │
├──────────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────────────────────┐ │
│  │                     FastAPI  (backend/main.py)                           │ │
│  │    • POST /chat            non-streaming JSON                            │ │
│  │    • POST /chat/stream     SSE — user_echo, tool_call, tool_result,      │ │
│  │                                  assistant_delta, assistant, done, error │ │
│  │    • GET  /models          lists available model ids                     │ │
│  │    • GET  /health          ollama reachability + model availability      │ │
│  │    • GET  /                friendly pointer to the UI                    │ │
│  └─────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
                                       │
                      calls intent_parser.handle_user_message()
                                       │
┌──────────────────────────────────────────────────────────────────────────────┐
│                             ORCHESTRATOR LAYER                                │
├──────────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────────────────────┐ │
│  │                 backend/agent/intent_parser.py                           │ │
│  │                                                                          │ │
│  │   1. looks_like_note_op(text)     → decides whether to expose tools      │ │
│  │   2. commit_intent detection      → forces confirm=true where needed     │ │
│  │   3. build_messages(state)        → [system, (context), ...history]     │ │
│  │   4. llm_handler.chat(...)        → LLMResponse                          │ │
│  │   5. if kind == "tool_calls" → run_tool_call() in a bounded loop         │ │
│  │   6. if kind == "message"   → emit assistant text, done                  │ │
│  │                                                                          │ │
│  │   Bounded by settings.max_tool_hops (default 5).                         │ │
│  └─────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
                       │                              │
              LLM provider                       Tool dispatcher
                       │                              │
                       ▼                              ▼
┌──────────────────────────────────────┐   ┌──────────────────────────────────┐
│           LLM LAYER                   │   │          TOOL LAYER              │
├──────────────────────────────────────┤   ├──────────────────────────────────┤
│  backend/agent/llm_handler.py         │   │  backend/tools/schemas.py        │
│    dispatches on MODEL_OPTIONS        │   │    Pydantic arg models           │
│           │                           │   │    ToolResult envelope           │
│    ┌──────┴──────┐                    │   │    TOOL_DEFS (JSON Schema)       │
│    ▼             ▼                    │   │                                  │
│ llm_ollama   llm_gemini               │   │  backend/tools/note_tools.py     │
│ (local)      (google-genai)           │   │    execute(name, args) → ToolRes │
│                                       │   │    validates, calls service,     │
│ Both return the same LLMResponse      │   │    catches all exceptions        │
│ shape — the orchestrator is           │   │                                  │
│ provider-agnostic.                    │   └─────────────────┬────────────────┘
└──────────────────────────────────────┘                     │
                                                             ▼
                                           ┌──────────────────────────────────┐
                                           │         SERVICE LAYER            │
                                           ├──────────────────────────────────┤
                                           │  backend/services/note_service.py│
                                           │    create / get / update / del   │
                                           │    list / list_tags              │
                                           │    search_semantic               │
                                           │    backfill_embeddings           │
                                           │                                  │
                                           │  backend/services/embeddings.py  │
                                           │    embed(text) → np.ndarray      │
                                           │    cosine(a, b) → float          │
                                           │    to_blob / from_blob           │
                                           └─────────────────┬────────────────┘
                                                             │
                                                             ▼
                                           ┌──────────────────────────────────┐
                                           │          DATA LAYER              │
                                           ├──────────────────────────────────┤
                                           │  backend/db/sqlite.py            │
                                           │    tx() context manager          │
                                           │    init_db() on startup          │
                                           │                                  │
                                           │  SQLite file at DB_PATH          │
                                           │  Single-table schema: notes      │
                                           │  Embeddings as float32 BLOBs     │
                                           └──────────────────────────────────┘
```

## Core Components

### HTTP Layer (`backend/main.py`)

The only code that knows about HTTP. Three responsibilities:

1. **Route definitions** — `/chat`, `/chat/stream`, `/models`, `/health`, `/`.
2. **Request/response DTOs** — `ChatIn`, `ChatOut`, `ChatToolCall` (Pydantic).
3. **Async → sync bridge** for the SSE endpoint — the orchestrator is synchronous (it blocks on network calls to Ollama / Gemini), so streaming wraps it in a worker thread and bridges events through a thread-safe `queue.Queue` into an iterator that `StreamingResponse` drains into the HTTP response body.

```python
# backend/main.py — non-streaming endpoint
@app.post("/chat", response_model=ChatOut)
def chat(body: ChatIn) -> ChatOut:
    turn = intent_parser.handle_user_message(
        body.session_id, body.message, model=_resolved_model(body.model)
    )
    return ChatOut(
        session_id=body.session_id,
        reply=turn.reply,
        tool_calls=[
            ChatToolCall(
                id=tc.id,
                name=tc.name,
                arguments=tc.arguments,
                result=tc.result.model_dump(mode="json"),
            )
            for tc in turn.tool_calls
        ],
    )
```

### Orchestrator (`backend/agent/intent_parser.py`)

The brain of a single user turn. Called once per inbound message; returns once the LLM emits a plain message (or `MAX_TOOL_HOPS` is reached).

Responsibilities:

- Decide whether tools should be exposed to the model this turn (**intent gate**).
- Detect explicit commit intent during a pending confirmation (**commit-intent gate**).
- Assemble the message list sent to the model (system prompt + context line + rolling history).
- Run the tool-calling loop with a hard upper bound.
- Persist every assistant message, tool call, and tool result back into session state.
- Emit progressive SSE events when a callback is supplied.

### LLM Layer (`backend/agent/llm_handler.py` + provider modules)

The dispatcher is a thin mapping from a public `model` string to a provider module:

| `model` id            | Provider  | Concrete model                         |
|-----------------------|-----------|----------------------------------------|
| `ollama`              | Ollama    | `settings.ollama_model` (env default)  |
| `ollama-llama3.2`     | Ollama    | `llama3.2`                             |
| `gemini-2.5-pro`      | Gemini    | `gemini-2.5-pro`                       |
| `gemini-2.5-flash`    | Gemini    | `gemini-2.5-flash`                     |

Unknown ids fall back to `DEFAULT_MODEL = "ollama"`. The orchestrator only ever sees the normalized `LLMResponse` shape — provider-specific translation happens inside each `llm_<provider>.py`. See [02 - Data Models](./02-data-models.md) for the full mapper tables.

### Tool Layer (`backend/tools/*`)

Two files with different jobs:

- `schemas.py` — **Pydantic argument models** + the `ToolResult` envelope + `TOOL_DEFS` (derived from `model_json_schema()`, sent to the LLM). No business logic.
- `note_tools.py` — **dispatcher**. Validates arguments, calls the service, wraps any exception in a `ToolResult(ok=False)`. Never raises.

```python
# backend/tools/note_tools.py — the contract
def execute(name: str, raw_args: dict | None) -> ToolResult:
    """Run a tool and return a ToolResult. Never raises."""
```

### Service Layer (`backend/services/*`)

Pure note-taking business logic. Takes primitives, returns Pydantic models, raises typed exceptions. Never imports FastAPI, Ollama, Gemini, or anything from `agent/`.

- `note_service.py` — CRUD, listing, semantic search, embedding backfill.
- `embeddings.py` — calls `nomic-embed-text` via Ollama, cosine similarity, blob packing.
- `models.py` — `Note`, `NoteSummary`, `TagCount` DTOs.

### Data Layer (`backend/db/sqlite.py`)

Raw `sqlite3`. No ORM. Two public surfaces:

- `init_db()` — run once on startup, idempotent via `CREATE TABLE IF NOT EXISTS`.
- `tx()` — context manager that yields a connection, commits on success, rolls back on exception, always closes.

See [02 - Data Models](./02-data-models.md) for the schema.

## Data Flow — One Chat Turn

The most important diagram in this document. Trace every layer a user message passes through.

```
 USER
   │  types "find my meeting note"
   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ FRONTEND (app/src/app/lib/api.ts)                                         │
│   POST /chat/stream { session_id, message, model }                        │
└──────────────────────────────────────────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ HTTP (backend/main.py)                                                    │
│   chat_stream(body) → StreamingResponse(_sse_stream(...))                 │
│   spawns a worker thread running handle_user_message(emit=queue.put)      │
└──────────────────────────────────────────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ ORCHESTRATOR (backend/agent/intent_parser.py)                             │
│   state = store.get(session_id)                                           │
│   state.messages.append({"role": "user", "content": user_text})           │
│   emit("user_echo", {message})                                            │
│                                                                           │
│   allow_tools = looks_like_note_op(text) or state.pending_confirmation    │
│   force_confirm = pending_confirmation is add/update AND commit intent    │
│                                                                           │
│   for _ in range(MAX_TOOL_HOPS):                                          │
│     messages = [system_prompt, (context), *state.messages]                │
│     resp = llm_handler.chat(messages, tools=..., on_delta=fwd)            │
│                                                                           │
│     if resp.kind == "message":                                            │
│         emit("assistant", {content}); emit("done", {}); return            │
│                                                                           │
│     for call in resp.tool_calls:                                          │
│         emit("tool_call", {...running})                                   │
│         result = run_tool_call(state, call, force_confirm)                │
│         emit("tool_result", {...ok|fail|needs_confirmation})              │
└──────────────────────────────────────────────────────────────────────────┘
   │                               │
   ▼                               ▼
┌──────────────┐       ┌──────────────────────────────────────────────────┐
│ LLM LAYER    │       │ TOOL DISPATCHER (backend/tools/note_tools.py)    │
│ llm_ollama   │       │   args = ArgsModel.model_validate(raw)           │
│    OR        │       │   two-step gate: if not args.confirm → preview   │
│ llm_gemini   │       │   otherwise → note_service.<operation>()         │
│              │       │   wrap result or exception in ToolResult         │
│ both return  │       └──────────────────────────┬───────────────────────┘
│ LLMResponse  │                                  │
└──────────────┘                                  ▼
                               ┌──────────────────────────────────────────┐
                               │ SERVICE (backend/services/note_service)  │
                               │   create_note / get_note / update_note / │
                               │   delete_note / list_notes / list_tags / │
                               │   search_semantic                        │
                               │                                          │
                               │   embeddings.embed(text) when needed     │
                               └──────────────────────┬───────────────────┘
                                                      │
                                                      ▼
                               ┌──────────────────────────────────────────┐
                               │ DATA (backend/db/sqlite.py)              │
                               │   with tx() as conn: conn.execute(...)   │
                               └──────────────────────────────────────────┘
```

After each tool result, the loop re-enters the orchestrator: `remember_referenced()` updates the pronoun-resolution ids, the pending-confirmation flag is set or cleared, and the assistant's tool call + the tool response are appended to `state.messages`. On the next hop the model sees the full trace and either calls another tool or produces the final text reply.

## Streaming Architecture — Sync Orchestrator, Async Response

The orchestrator is intentionally synchronous: it blocks on `ollama.Client.chat` and `google.genai.generate_content_stream`, both of which are blocking clients. FastAPI's streaming response, however, drives an **asynchronous** iterator. The bridge lives in `backend/main.py`:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  FastAPI request handler (async)                                         │
│    StreamingResponse(_sse_stream(session_id, message, model), ...)       │
└─────────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  _sse_stream(...)  — a *sync* generator                                  │
│    q = queue.Queue()                                                      │
│    spawn Thread(target=run_orchestrator)                                  │
│    while True:                                                            │
│        item = q.get()                                                     │
│        if item is sentinel: break                                         │
│        yield format_sse(event, data)                                      │
└─────────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Worker thread                                                            │
│    handle_user_message(session_id, message, emit=q.put, model=...)       │
│    — orchestrator calls emit(event, data) at every stage                  │
│    finally: q.put(sentinel)                                               │
└─────────────────────────────────────────────────────────────────────────┘
```

This pattern keeps the orchestrator simple (plain sync code, easy to test, easy to reason about) while still delivering a proper streaming experience to the browser.

**Event names are a stable contract** with the frontend (`app/src/app/lib/api.ts`):

| Event              | Payload                                                            |
|--------------------|--------------------------------------------------------------------|
| `user_echo`        | `{ message: string }`                                              |
| `tool_call`        | `{ id, name, arguments, status: "running" }`                       |
| `tool_result`      | `{ id, status, message, error_code, data, needs_confirmation, ... }` |
| `assistant_delta`  | `{ content: string }` — one token or chunk at a time               |
| `assistant`        | `{ content: string }` — final authoritative text                   |
| `error`            | `{ message: string }` — orchestrator crashed                       |
| `done`             | `{}` — end of turn sentinel                                        |

The header `X-Accel-Buffering: no` is set to prevent nginx / Cloudflare from buffering the response.

## Non-Streaming Path

`POST /chat` calls `handle_user_message()` without an `emit` callback. It runs the exact same loop, collects `TurnToolCall` records into a `TurnResult`, and returns them in one shot as JSON. This path is used by programmatic callers and tests; the UI always uses `/chat/stream`.

## Provider Independence

`llm_handler.chat` is a pure dispatcher — it routes to `llm_ollama.chat` or `llm_gemini.chat` based on the `model` string, and both providers return the same normalized `LLMResponse`:

```python
class LLMResponse(BaseModel):
    kind: Literal["tool_calls", "message"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: dict | None = None
```

Everything above the provider layer (the orchestrator, the tool dispatcher, the services) is **provider-agnostic**. Adding a new provider requires three things:

1. A new `backend/agent/llm_<name>.py` that exposes `chat(messages, *, model, tools, on_delta) -> LLMResponse`.
2. An entry in `llm_handler.MODEL_OPTIONS` mapping a public id to `("<name>", "<concrete-model>")`.
3. An entry in `app/src/app/types.ts` `MODEL_OPTIONS` so the UI can pick it.

See [02 - Data Models](./02-data-models.md) for the message / tool-schema mappers that sit inside each provider module.

## Configuration

All runtime knobs live in `backend/config.py` — a frozen dataclass populated from environment variables via `python-dotenv`. The single module ensures no code elsewhere reaches into `os.environ` directly.

```python
@dataclass(frozen=True)
class Settings:
    ollama_host: str        = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str       = os.getenv("OLLAMA_MODEL", "llama3.1")
    ollama_embed_model: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    db_path: str            = os.getenv("DB_PATH", "./data/notes.db")
    max_tool_hops: int      = int(os.getenv("MAX_TOOL_HOPS", "5"))
    history_turns: int      = int(os.getenv("HISTORY_TURNS", "20"))
    search_threshold: float = float(os.getenv("SEARCH_THRESHOLD", "0.35"))
    search_fallback_limit: int = int(os.getenv("SEARCH_FALLBACK_LIMIT", "3"))
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY") or None
```

`settings.max_tool_hops` gates the orchestrator loop — it is never removed or bypassed.

## Startup

`main.py` wires a FastAPI `lifespan` handler that runs two things once, before the first request:

1. `init_db()` — creates the `notes` table and its indices if they don't exist.
2. `note_service.backfill_embeddings()` — embeds any row whose `embedding` column is `NULL`. Safe to re-run; idempotent.

A missing embedding service (e.g. Ollama is down) is logged and skipped — the app stays up. Un-embedded rows will be backfilled the next time they're touched by a write or by another startup pass.

## File Map

```
backend/
├── main.py                  ← HTTP layer (FastAPI)
├── config.py                ← frozen Settings dataclass
├── agent/
│   ├── intent_parser.py     ← orchestrator loop + intent/commit gates
│   ├── conversation_state.py← SessionStore, SessionState, context line
│   ├── prompts.py           ← SYSTEM_PROMPT (single source of truth)
│   ├── llm_handler.py       ← provider dispatcher (MODEL_OPTIONS)
│   ├── llm_ollama.py        ← Ollama provider
│   ├── llm_gemini.py        ← Gemini provider (schema + message translation)
│   └── llm_types.py         ← ToolCall, LLMResponse (normalized)
├── tools/
│   ├── schemas.py           ← Pydantic arg models, ToolResult, TOOL_DEFS
│   └── note_tools.py        ← dispatcher (execute) — wraps exceptions
├── services/
│   ├── note_service.py      ← CRUD, search, backfill
│   ├── embeddings.py        ← nomic-embed-text, cosine, blob (de)serialise
│   └── models.py            ← Note, NoteSummary, TagCount
└── db/
    └── sqlite.py            ← schema, init_db, tx() context manager
```

## Invariants

Rules enforced by the codebase itself, not by convention:

| Invariant                                           | Where enforced                                 |
|-----------------------------------------------------|-----------------------------------------------|
| Tool loop is bounded                                | `intent_parser.py` — `for _ in range(settings.max_tool_hops)` |
| LLM can't skip the confirmation gate                | `note_tools.py` — `_add_note`, `_update_note`, `_delete_note` check `args.confirm` before touching the DB |
| Tool dispatcher never raises                        | `note_tools.execute()` has a catch-all `except Exception` |
| Providers return the same shape                     | `LLMResponse` is the only return type from `llm_*.chat()` |
| All SQL goes through `tx()`                         | Services only import `tx` — nothing else opens a connection |
| No embedding work at import time                    | `embeddings._get_client()` is lazy; tests can monkeypatch the client before use |

## Related Docs

- [02 - Data Models](./02-data-models.md) — Pydantic models, provider mappers, SQLite schema.
- [03 - Note Agent](./03-note-agent.md) — tools, confirmation gates, orchestrator flows.
- [04 - Memory and State](./04-memory-and-state.md) — session store, context line, pronoun resolution.
