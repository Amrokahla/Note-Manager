# Conversational Note-Taking Agent — Implementation Plan

> TechLabs London — AI Engineer Technical Assessment
> Model: **llama3.2** via **Ollama** (local inference)
> Language: **Python** backend, lightweight web UI under `/app`

---

## 1. Goal & Evaluation Lens

Build a chat-based agent that lets a user fully manage personal notes through natural language: **Create, Read/Search, Update, Delete, and Reason over** notes.

The graders explicitly say they care about:

1. **Tool/function schema design** — clean, well-typed interfaces.
2. **State management** — multi-turn awareness and references to prior turns.
3. **Edge cases** — disambiguation, destructive-action confirmation, graceful failures.
4. **Evaluation harness** — automated pass/fail on 10–15 conversational scenarios.
5. **Design reasoning** — we must be able to defend every decision.

Everything below is shaped by those five priorities.

---

## 2. High-Level Architecture

```
          ┌────────────────────────────────────────────────────┐
          │                    app/ (UI)                       │
          │         minimal chat UI (React or HTMX)            │
          └──────────────────────┬─────────────────────────────┘
                                 │ HTTP (JSON) / SSE
                                 ▼
          ┌────────────────────────────────────────────────────┐
          │                 backend/main.py                    │
          │   FastAPI app: /chat, /notes (debug), /health      │
          └──────────────────────┬─────────────────────────────┘
                                 │
                                 ▼
          ┌────────────────────────────────────────────────────┐
          │               backend/agent/                       │
          │  ┌─────────────────┐  ┌──────────────────────────┐ │
          │  │ llm_handler.py  │  │ conversation_state.py    │ │
          │  │ (Ollama client) │  │ (per-session memory)     │ │
          │  └────────┬────────┘  └────────────┬─────────────┘ │
          │           │                        │               │
          │  ┌────────▼────────────────────────▼────────────┐  │
          │  │            intent_parser.py                  │  │
          │  │  decides: tool_call | clarify | answer       │  │
          │  └──────────────────────┬───────────────────────┘  │
          └─────────────────────────┼──────────────────────────┘
                                    ▼
          ┌────────────────────────────────────────────────────┐
          │                backend/tools/                      │
          │   schemas.py (Pydantic)   note_tools.py (dispatch) │
          └──────────────────────┬─────────────────────────────┘
                                 ▼
          ┌────────────────────────────────────────────────────┐
          │              backend/services/                     │
          │         note_service.py (business logic)           │
          └──────────────────────┬─────────────────────────────┘
                                 ▼
          ┌────────────────────────────────────────────────────┐
          │                 backend/db/                        │
          │       sqlite.py (connection + migrations)          │
          └────────────────────────────────────────────────────┘
```

**Layer responsibilities (strict, no skipping layers):**

| Layer | Responsibility | Does NOT |
|---|---|---|
| `agent/` | Talk to the LLM, manage dialogue state, pick a tool | Touch DB directly |
| `tools/` | Validate arguments, dispatch to services, format results | Contain business logic |
| `services/` | Note business rules (search ranking, confirmation gating) | Know about LLM or UI |
| `db/` | Raw SQLite access, schema, migrations | Know about notes semantics beyond rows |

This separation is the single biggest design lever for scoring well on "tool design" and "decomposition."

---

## 3. Model Integration — llama3.2 via Ollama

### 3.1 Why llama3.2 + Ollama
- **Runs fully local** → no API keys, no cost, reproducible for the grader.
- **llama3.2 (3B)** supports tool/function calling natively in Ollama (`/api/chat` with `tools` field).
- Fast enough on a laptop to keep the chat latency reasonable.

### 3.2 How we'll call it
- Use the official `ollama` Python package (`pip install ollama`).
- One call style: **`ollama.chat(model="llama3.2", messages=[...], tools=[...])`**.
- The model returns either:
  - `message.content` → plain assistant text (clarifying question or final answer), OR
  - `message.tool_calls` → structured tool invocations we must execute and feed back.

### 3.3 Fallback / robustness
- llama3.2's tool-calling is good but not perfect at 3B. We add a **guarded JSON-repair layer** in `intent_parser.py`: if `tool_calls` is missing but the assistant text looks like JSON describing a tool call, we re-parse it. This is documented as a deliberate robustness measure, not a hack.

---

## 4. File-by-File Responsibilities

### `backend/main.py`
- FastAPI entrypoint.
- Endpoints:
  - `POST /chat` → `{ session_id, message }` → streams or returns assistant reply.
  - `GET /notes` (debug only) → list raw notes for inspection.
  - `GET /health` → checks Ollama is reachable and DB is writable.
- Mounts `/app` static build (or serves a single `index.html`).

### `backend/agent/llm_handler.py`
- Thin wrapper around `ollama.chat`.
- Responsibilities:
  - Inject the **system prompt** (defines agent persona, rules, confirmation policy).
  - Pass the tool schemas from `tools/schemas.py`.
  - Return a normalized object: `{kind: "tool_call" | "message", ...}`.
- Configurable via env: `OLLAMA_HOST`, `OLLAMA_MODEL` (default `llama3.2`).

### `backend/agent/conversation_state.py`
- Per-`session_id` state: message history, last-referenced note IDs ("that note", "the last one"), pending confirmations.
- In-memory dict for v1; interface lets us swap to Redis later.
- Caps history length and summarizes older turns (we'll keep it simple: sliding window of N=20 turns).

### `backend/agent/intent_parser.py`
- Orchestration loop:
  1. Build messages (system + history + user).
  2. Call `llm_handler`.
  3. If tool call → validate args via Pydantic, execute via `tools/note_tools.py`, append tool result, loop.
  4. If plain message → return to user.
  5. Hard-stop after `MAX_TOOL_HOPS = 5` to prevent runaway loops.
- Owns the **confirmation state machine** for destructive ops (see §6).

### `backend/tools/schemas.py`
- Pydantic models for every tool's arguments and returns. One source of truth.
- Also exports an **Ollama-compatible JSON schema** for each tool (generated via `model_json_schema()`).
- Tools defined:
  - `add_note(title: str, body: str, tags: list[str] = [])`
  - `search_notes(query: str, tags: list[str] = [], date_from: str | None, date_to: str | None, limit: int = 10, semantic: bool = False)`
  - `get_note(note_id: int)`
  - `update_note(note_id: int, title: str | None, body: str | None, tags: list[str] | None)`
  - `delete_note(note_id: int, confirm: bool = False)`
  - `list_recent(limit: int = 5)`
  - `summarize_notes(note_ids: list[int])` (agent can then reason in natural language)

### `backend/tools/note_tools.py`
- Dispatcher: `def execute(tool_name, args) -> dict`.
- Validates args through `schemas.py`, calls `note_service`, returns a **tool-result dict** that the LLM can consume (`{"ok": true, "data": ..., "message": "..."}`).

### `backend/services/note_service.py`
- Pure business logic, no LLM, no HTTP.
- Functions: `create`, `search` (keyword + tags + date range), `get`, `update`, `delete`, `list_recent`.
- Search: SQL `LIKE` + tag join for v1. Semantic search is a **bonus** switch (see §8).
- Enforces: a destructive op requires `confirm=True`, otherwise returns `{"ok": false, "needs_confirmation": true, "preview": {...}}`.

### `backend/db/sqlite.py`
- Single SQLite file at `./data/notes.db`.
- Schema (migration run at boot):
  ```sql
  CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  );
  CREATE TABLE tags (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (note_id, tag)
  );
  CREATE VIRTUAL TABLE notes_fts USING fts5(title, body, content='notes', content_rowid='id');
  ```
- FTS5 gives us fast keyword search for free; `tags` table keeps tag queries indexable.
- **Why SQLite:** zero-config, file-based, survives restarts, trivial for the grader to inspect with `sqlite3`.

### `backend/eval/test_cases.py`
- 12–15 scripted scenarios, each a list of user turns + expected assertions on:
  - which tool was called,
  - with which arguments (fuzzy match on strings),
  - and/or final DB state.
- Scenarios cover: happy paths, ambiguity, confirmation, multi-turn reference, no-results, invalid input, contradictory notes.
- Produces a markdown report: pass/fail + reason per scenario.

### `app/` (UI)
- **Minimal** single-page chat (the brief says UI isn't evaluated).
- Options, pick one at build time:
  1. **HTMX + Jinja** (simplest, zero build step) — *preferred*.
  2. Small Vite + React if we want to look a bit nicer.
- Features: message list, input box, session reset button, optional "show note DB" debug panel.

---

## 5. Tool Schema Design (core evaluation criterion)

Principles we'll follow and document in the README:

1. **One tool = one intent.** No mega-tool that branches internally.
2. **Explicit typed arguments**, never free-form JSON blobs.
3. **Idempotent where possible.** `update_note` takes optional fields so the agent can patch safely.
4. **Destructive ops carry a `confirm` boolean.** Server-side enforced, not trusted from the LLM alone — the *agent orchestrator* gates this with an explicit user "yes" in the previous turn.
5. **Return shapes are uniform:** `{ok, data?, message, needs_confirmation?, candidates?}`. `candidates` is how we surface "multiple notes matched — which one?" back to the model.
6. **Search is one tool with flags**, not three separate tools, because that matches how users phrase requests.

---

## 6. Handling the Required Behaviours

| Requirement | How we implement it |
|---|---|
| Intent disambiguation | `search_notes` returns `candidates` when >1 match; system prompt instructs the model: *if you receive `candidates`, ask the user to pick — never guess.* Eval harness tests this explicitly. |
| Confirmation on destructive actions | Two-step: (1) model calls `delete_note(confirm=False)` → service returns preview + `needs_confirmation`; (2) model asks user; (3) on "yes," model calls `delete_note(confirm=True)`. Enforced in `note_service`. |
| Multi-turn awareness | `conversation_state` stores `last_referenced_note_ids`. System prompt teaches the model to resolve "that note," "the last one," "it." Also: full message history passed on every turn (bounded). |
| Graceful error handling | Every tool returns a structured error; system prompt mandates: *if `ok=false`, explain in plain English and suggest next steps (e.g. "no notes matched 'standup' — want me to list recent ones?")*. |
| Evaluation harness | `eval/test_cases.py` — see §7. |

---

## 7. Evaluation Harness (15 scenarios)

Mix of happy path (H), edge case (E), and destructive (D):

1. (H) Add a simple note with tags.
2. (H) Search by keyword returns the right note.
3. (H) Search by tag.
4. (H) List recent notes.
5. (E) Search returns zero results → agent offers alternatives.
6. (E) Ambiguous reference ("update *the* note about the meeting") with 2 matches → agent asks which.
7. (H) Multi-turn reference: add a note, then "add 'Tuesday' to that note" → correct `update_note` call.
8. (D) "Delete the note about the old office" → agent asks to confirm, user says yes → deleted.
9. (D) Same as 8 but user says "no" → note remains.
10. (E) Update a non-existent note ID → graceful failure message.
11. (H) Reason across notes: "summarise my urgent notes" → calls `search_notes(tags=["urgent"])` then summarises.
12. (E) Contradiction probe: two notes disagreeing → agent flags the conflict.
13. (E) Malformed tag input ("tag it #foo, #bar!") → tags stored cleanly.
14. (H) Date-range search: "what did I write last week?"
15. (E) Tool loop guard: craft a prompt that tries to force infinite tool calls → orchestrator stops at `MAX_TOOL_HOPS`.

**Harness mechanics:**
- Runs the full agent loop against an in-memory SQLite.
- Mocks Ollama? **No** — we hit real llama3.2. This makes tests slower but meaningful. We'll keep a `--fast` flag that uses a stubbed deterministic LLM for CI.
- Assertions are a mix of: tool-call sequence match, substring-in-reply, DB state.
- Output: `eval/report.md` with a table and overall pass rate.

---

## 8. Bonus Challenges (do after core is green)

Priority order (highest ROI first):

1. **Containerisation** (easiest win): `Dockerfile` for backend + `docker-compose.yml` bringing up `ollama` + backend + static UI. One-command run for the grader.
2. **Semantic search**: add a `embeddings` column (BLOB) + Ollama's `nomic-embed-text` model. Toggle via `search_notes(semantic=True)`. Document in README why `nomic-embed-text` (open, small, good quality).
3. **Multi-user isolation**: add `user_id` to the notes/tags schema, pass `X-User-Id` header (stubbed auth), scope all service queries. Document strategy.
4. **MCP server**: wrap `note_tools` as an MCP server using the official Python MCP SDK — same tool schemas, new transport. Prove tools are reusable.

Each bonus lives behind a clean boundary so partial completion still ships cleanly.

---

## 9. Milestones & Sequencing

Aimed at ~72h budget; each milestone is independently shippable.

| # | Milestone | Output |
|---|---|---|
| M0 | Skeleton + deps | `requirements.txt`, FastAPI hello-world, Ollama health check |
| M1 | DB + services | `sqlite.py` + `note_service.py` + unit tests for CRUD |
| M2 | Tool schemas + dispatcher | `schemas.py` + `note_tools.py` + round-trip tests |
| M3 | LLM loop (no confirmation yet) | End-to-end: "add a note about X" works in CLI |
| M4 | State + multi-turn + disambiguation | "that note" references resolve |
| M5 | Destructive confirmation flow | Delete requires yes/no |
| M6 | Eval harness (15 scenarios) | `eval/report.md` generated |
| M7 | README + tool-schema docs | Submission-ready |
| B1–B4 | Bonuses | As time allows |

---

## 10. Dependencies (proposed `requirements.txt`)

```
fastapi
uvicorn[standard]
pydantic>=2
ollama
python-dotenv
# Semantic search (bonus)
numpy
# Eval
pytest
rich
```

No ORM: raw SQL via `sqlite3` stdlib is enough here and keeps the DB layer trivially auditable.

---

## 11. Configuration (`.env`)

```
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2
DB_PATH=./data/notes.db
MAX_TOOL_HOPS=5
HISTORY_TURNS=20
```

---

## 12. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| llama3.2 3B misuses tool schema | JSON-repair fallback in `intent_parser`; tight system prompt; schema kept small and flat |
| Tool-call infinite loops | `MAX_TOOL_HOPS` hard cap |
| Ambiguity leaks past the model (it guesses) | Server-side `candidates` return + eval scenario #6 locks the behaviour |
| Destructive action without confirmation | Enforced in `note_service`, not trusted from the LLM |
| Slow eval because real Ollama | `--fast` stubbed-LLM mode for CI; full mode for final report |

---

## 13. Submission Checklist

- [ ] Source code pushed to GitHub with a clean history.
- [ ] `README.md`: setup, `.env`, `docker compose up`, how to run eval.
- [ ] `docs/TOOLS.md`: every tool, args, returns, examples.
- [ ] `docs/DESIGN.md`: one-pager expanding on decisions here.
- [ ] `eval/report.md`: latest pass/fail run.
- [ ] Optional: short Loom/video walkthrough.

---

**North star:** every design choice in this doc is something we can defend in an interview in one sentence. If we can't, we simplify it.

---

# Part II — Detailed Build Phases

Each phase below is a concrete, self-contained unit of work. Every phase lists: **what we build, the data/logic, representative code, and the "definition of done."** Code snippets are intent-level (they show structure and key logic, not every line of the final implementation).

---

## Phase 0 — Project Skeleton & Dependency Setup

**Goal:** a runnable FastAPI server that talks to Ollama and confirms the model is reachable.

### 0.1 What we build

- `requirements.txt` at repo root.
- `.env.example` committed, `.env` gitignored.
- `backend/main.py` with `/health` endpoint.
- `backend/config.py` — centralized env loader.

### 0.2 `backend/config.py`

```python
from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.2")
    db_path: str = os.getenv("DB_PATH", "./data/notes.db")
    max_tool_hops: int = int(os.getenv("MAX_TOOL_HOPS", "5"))
    history_turns: int = int(os.getenv("HISTORY_TURNS", "20"))

settings = Settings()
```

**Why a frozen dataclass:** immutable, importable from anywhere, trivially testable by overriding attributes in fixtures.

### 0.3 `backend/main.py` (initial)

```python
from fastapi import FastAPI
from ollama import Client
from backend.config import settings

app = FastAPI(title="Note Agent")
ollama = Client(host=settings.ollama_host)

@app.get("/health")
def health():
    try:
        models = ollama.list()
        ok = any(settings.ollama_model in m["name"] for m in models.get("models", []))
        return {"ok": ok, "model": settings.ollama_model}
    except Exception as e:
        return {"ok": False, "error": str(e)}
```

### 0.4 Definition of Done
- `uvicorn backend.main:app --reload` serves `/health` returning `{"ok": true, "model": "llama3.2"}`.
- `ollama pull llama3.2` has completed at least once.

---

## Phase 1 — Database Layer (`backend/db/sqlite.py`)

**Goal:** durable, inspectable storage with fast keyword search via FTS5.

### 1.1 Data Model

Three logical entities, three tables:

| Table | Purpose |
|---|---|
| `notes` | one row per note |
| `tags` | many tags per note (normalized) |
| `notes_fts` | FTS5 virtual table mirroring `title` + `body` for search |

### 1.2 Schema

```sql
CREATE TABLE IF NOT EXISTS notes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT    NOT NULL,
  body       TEXT    NOT NULL,
  created_at TEXT    NOT NULL,
  updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
  note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  tag     TEXT    NOT NULL,
  PRIMARY KEY (note_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
USING fts5(title, body, content='notes', content_rowid='id');

-- Keep FTS in sync via triggers
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO notes_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
```

### 1.3 `backend/db/sqlite.py` (shape)

```python
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from backend.config import settings

SCHEMA_SQL = "..."  # the SQL above

def _connect() -> sqlite3.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA_SQL)

@contextmanager
def tx():
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

**Why raw `sqlite3`:** no ORM magic; the grader can read every query; total LOC for DB layer ≈ 60.

### 1.4 Definition of Done
- `init_db()` is called on FastAPI startup.
- `sqlite3 data/notes.db ".schema"` shows all tables, triggers, and the FTS virtual table.

---

## Phase 2 — Note Service (`backend/services/note_service.py`)

**Goal:** all note business logic in one place, with no awareness of HTTP or LLMs.

### 2.1 Domain Models

Pydantic models used both internally and as tool return types:

```python
# backend/services/models.py
from datetime import datetime
from pydantic import BaseModel, Field

class Note(BaseModel):
    id: int
    title: str
    body: str
    tags: list[str] = []
    created_at: datetime
    updated_at: datetime

class NoteSummary(BaseModel):
    """Compact form used for search results and candidate lists."""
    id: int
    title: str
    snippet: str
    tags: list[str]
    updated_at: datetime
```

### 2.2 Core functions (signatures)

```python
def create_note(title: str, body: str, tags: list[str]) -> Note: ...
def get_note(note_id: int) -> Note | None: ...
def update_note(note_id: int,
                title: str | None,
                body: str | None,
                tags: list[str] | None) -> Note | None: ...
def delete_note(note_id: int) -> bool: ...
def list_recent(limit: int = 5) -> list[NoteSummary]: ...
def search_notes(query: str | None,
                 tags: list[str] | None,
                 date_from: datetime | None,
                 date_to: datetime | None,
                 limit: int = 10) -> list[NoteSummary]: ...
```

### 2.3 Search logic (the interesting part)

We merge three filters: FTS keyword match, tag membership, and date range.

```python
def search_notes(query, tags, date_from, date_to, limit=10):
    where, args = [], []
    join = ""
    if query:
        join += " JOIN notes_fts f ON f.rowid = n.id "
        where.append("notes_fts MATCH ?")
        args.append(_to_fts_query(query))   # e.g. "deadline OR project"
    if tags:
        join += """ JOIN tags t ON t.note_id = n.id """
        where.append(f"t.tag IN ({','.join('?' * len(tags))})")
        args += tags
    if date_from:
        where.append("n.created_at >= ?"); args.append(date_from.isoformat())
    if date_to:
        where.append("n.created_at <= ?"); args.append(date_to.isoformat())

    sql = f"""
      SELECT DISTINCT n.id, n.title, substr(n.body, 1, 200) AS snippet, n.updated_at
      FROM notes n {join}
      {"WHERE " + " AND ".join(where) if where else ""}
      ORDER BY n.updated_at DESC
      LIMIT ?
    """
    args.append(limit)
    # execute, hydrate tags per row, return list[NoteSummary]
```

**Tag normalization helper** (used on both write and search):

```python
def normalize_tag(t: str) -> str:
    return t.strip().lstrip("#").lower()
```

This quietly solves eval scenario #13 (malformed tag input).

### 2.4 Destructive-action preview

`delete_note` is kept honest: the agent calls it via a tool wrapper that first returns a preview, then a second call actually deletes. The service itself is simple; the gating lives in `tools/note_tools.py` (Phase 4) so the service stays reusable from tests and from a future MCP server.

### 2.5 Definition of Done
- `pytest backend/tests/test_note_service.py` is green for: create, get, update, delete, search-by-keyword, search-by-tag, search-by-date-range, tag normalization.

---

## Phase 3 — Tool Schemas (`backend/tools/schemas.py`)

**Goal:** the single source of truth for every tool the LLM is allowed to call. This is the highest-leverage file for scoring on "tool design."

### 3.1 Design Rules (enforced here)

1. One tool per user intent.
2. All arguments are typed; no free-form JSON.
3. Every tool has a uniform return envelope.
4. Destructive tools carry a `confirm: bool` argument.

### 3.2 Arg models

```python
from pydantic import BaseModel, Field
from datetime import datetime

class AddNoteArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list, max_length=20)

class SearchNotesArgs(BaseModel):
    query: str | None = Field(None, description="Keyword or natural-language query")
    tags: list[str] = Field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = Field(10, ge=1, le=50)
    semantic: bool = Field(False, description="Use vector similarity (bonus).")

class GetNoteArgs(BaseModel):
    note_id: int

class UpdateNoteArgs(BaseModel):
    note_id: int
    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None

class DeleteNoteArgs(BaseModel):
    note_id: int
    confirm: bool = False

class ListRecentArgs(BaseModel):
    limit: int = Field(5, ge=1, le=50)

class SummarizeNotesArgs(BaseModel):
    note_ids: list[int] = Field(..., min_length=1, max_length=20)
```

### 3.3 Uniform return envelope

```python
from typing import Any, Literal

class ToolResult(BaseModel):
    ok: bool
    message: str
    data: Any | None = None
    needs_confirmation: bool = False
    candidates: list[dict] | None = None   # for disambiguation
    error_code: Literal["not_found", "invalid_arg", "ambiguous",
                        "needs_confirmation", "internal"] | None = None
```

### 3.4 Ollama tool descriptors

Ollama expects OpenAI-style tool definitions. We generate them from Pydantic:

```python
def _tool(name: str, description: str, args_model: type[BaseModel]) -> dict:
    schema = args_model.model_json_schema()
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }

TOOL_DEFS = [
    _tool("add_note",        "Create a new note.",                    AddNoteArgs),
    _tool("search_notes",    "Search notes by keyword/tag/date.",     SearchNotesArgs),
    _tool("get_note",        "Fetch one note by id.",                 GetNoteArgs),
    _tool("update_note",     "Patch a note's title/body/tags.",       UpdateNoteArgs),
    _tool("delete_note",     "Delete a note; requires confirm=true.", DeleteNoteArgs),
    _tool("list_recent",     "List the N most recently updated notes.", ListRecentArgs),
    _tool("summarize_notes", "Fetch several notes for the model to summarise.", SummarizeNotesArgs),
]
```

### 3.5 Definition of Done
- `TOOL_DEFS` imported cleanly.
- A unit test validates each model round-trips a known-good JSON payload and rejects malformed ones.

---

## Phase 4 — Tool Dispatcher (`backend/tools/note_tools.py`)

**Goal:** take a `(tool_name, raw_args)` pair from the LLM, validate, execute, and return a `ToolResult`. This layer is where **confirmation gating** and **disambiguation** are enforced.

### 4.1 Dispatch map

```python
from backend.tools.schemas import (
    AddNoteArgs, SearchNotesArgs, GetNoteArgs, UpdateNoteArgs,
    DeleteNoteArgs, ListRecentArgs, SummarizeNotesArgs, ToolResult,
)
from backend.services import note_service

def execute(name: str, raw_args: dict) -> ToolResult:
    try:
        handler = _HANDLERS[name]
    except KeyError:
        return ToolResult(ok=False, message=f"Unknown tool: {name}",
                          error_code="invalid_arg")
    try:
        return handler(raw_args)
    except ValidationError as e:
        return ToolResult(ok=False, message=f"Invalid arguments: {e}",
                          error_code="invalid_arg")
    except Exception as e:
        return ToolResult(ok=False, message=f"Internal error: {e}",
                          error_code="internal")
```

### 4.2 Handler example — `delete_note` (two-step confirmation)

```python
def _delete_note(raw: dict) -> ToolResult:
    args = DeleteNoteArgs(**raw)
    note = note_service.get_note(args.note_id)
    if note is None:
        return ToolResult(ok=False, message=f"No note with id {args.note_id}.",
                          error_code="not_found")
    if not args.confirm:
        return ToolResult(
            ok=False,
            needs_confirmation=True,
            error_code="needs_confirmation",
            message=(f"About to delete note #{note.id} '{note.title}'. "
                     "Please confirm with the user before calling again with confirm=true."),
            data={"preview": note.model_dump(mode="json")},
        )
    note_service.delete_note(args.note_id)
    return ToolResult(ok=True, message=f"Deleted note #{args.note_id}.")
```

### 4.3 Handler example — `search_notes` (disambiguation)

```python
def _search_notes(raw: dict) -> ToolResult:
    args = SearchNotesArgs(**raw)
    results = note_service.search_notes(
        query=args.query, tags=args.tags,
        date_from=args.date_from, date_to=args.date_to,
        limit=args.limit,
    )
    if not results:
        return ToolResult(ok=True, message="No notes matched.", data=[])
    # If the user's phrasing implies a single target but we got >1 → flag ambiguity
    if args.limit == 1 and len(results) > 1:
        return ToolResult(
            ok=False, error_code="ambiguous",
            message="Multiple notes matched. Ask the user which one.",
            candidates=[r.model_dump(mode="json") for r in results],
        )
    return ToolResult(ok=True, message=f"Found {len(results)} note(s).",
                      data=[r.model_dump(mode="json") for r in results])
```

### 4.4 Definition of Done
- Each tool has a unit test asserting both happy path and at least one failure mode.
- `execute()` never raises — always returns a `ToolResult`.

---

## Phase 5 — LLM Handler (`backend/agent/llm_handler.py`)

**Goal:** wrap `ollama.chat` behind a stable interface and produce a normalized response the orchestrator can switch on.

### 5.1 System Prompt (committed to repo; a real artifact, not an afterthought)

Key rules baked in:

```
You are a helpful note-taking assistant.
- Use the provided tools to read or modify notes. Never invent note contents or ids.
- If a tool result has `needs_confirmation: true`, ask the user to confirm in plain English before calling the tool again with confirm=true.
- If a tool result has `candidates`, present them to the user and ask which one they mean. Do NOT pick one yourself.
- When a search returns nothing, say so and suggest an alternative (e.g. list recent notes).
- Keep replies concise.
```

### 5.2 Normalized response type

```python
from pydantic import BaseModel
from typing import Literal

class ToolCall(BaseModel):
    name: str
    arguments: dict

class LLMResponse(BaseModel):
    kind: Literal["tool_calls", "message"]
    content: str | None = None
    tool_calls: list[ToolCall] = []
    raw: dict | None = None
```

### 5.3 Call site

```python
def chat(messages: list[dict]) -> LLMResponse:
    resp = ollama.chat(
        model=settings.ollama_model,
        messages=messages,
        tools=TOOL_DEFS,
        options={"temperature": 0.2},
    )
    msg = resp["message"]
    if msg.get("tool_calls"):
        return LLMResponse(
            kind="tool_calls",
            tool_calls=[ToolCall(name=tc["function"]["name"],
                                 arguments=tc["function"].get("arguments", {}))
                        for tc in msg["tool_calls"]],
            raw=resp,
        )
    # JSON-repair fallback: sometimes 3B models emit tool calls as text JSON
    maybe = _try_parse_toolcall_from_text(msg.get("content", ""))
    if maybe is not None:
        return LLMResponse(kind="tool_calls", tool_calls=[maybe], raw=resp)
    return LLMResponse(kind="message", content=msg.get("content", ""), raw=resp)
```

### 5.4 Why `temperature=0.2`
We want the model to stick to tool calls over creative prose. Not zero, because deterministic-mode models sometimes loop on refusals.

### 5.5 Definition of Done
- Given a mocked Ollama that returns tool_calls, `chat()` yields a `kind="tool_calls"` response with validated arguments.
- Given a mocked reply containing stringified JSON tool call, the repair path converts it.

---

## Phase 6 — Conversation State (`backend/agent/conversation_state.py`)

**Goal:** per-session memory that makes "that note," "the last one," and multi-turn references work.

### 6.1 State Model

```python
from dataclasses import dataclass, field
from collections import deque

@dataclass
class SessionState:
    session_id: str
    messages: deque[dict] = field(default_factory=lambda: deque(maxlen=40))
    last_referenced_note_ids: list[int] = field(default_factory=list)
    pending_confirmation: dict | None = None   # e.g. {"tool": "delete_note", "args": {...}}

class SessionStore:
    def __init__(self):
        self._sessions: dict[str, SessionState] = {}

    def get(self, sid: str) -> SessionState:
        return self._sessions.setdefault(sid, SessionState(session_id=sid))
```

### 6.2 Reference resolution

After every successful tool call that returns notes, we update `last_referenced_note_ids`:

```python
def remember_referenced(state: SessionState, result: ToolResult) -> None:
    ids: list[int] = []
    if isinstance(result.data, list):
        ids = [row["id"] for row in result.data if "id" in row]
    elif isinstance(result.data, dict) and "id" in result.data:
        ids = [result.data["id"]]
    if ids:
        state.last_referenced_note_ids = ids
```

We surface this to the model as a **hidden system turn** injected just before each user message:

```
(context) The most recently referenced note ids are: [17, 18]. "that note" refers to 17.
```

This small trick dramatically improves multi-turn behaviour on 3B models.

### 6.3 History truncation

- `deque(maxlen=40)` keeps the last 40 messages (user + assistant + tool).
- The system prompt is prepended on each call, never stored in the deque.

### 6.4 Definition of Done
- A test simulating "add note … update that note" yields an `update_note` call with the correct id without it being mentioned by name.

---

## Phase 7 — Intent Parser / Orchestrator (`backend/agent/intent_parser.py`)

**Goal:** run the tool-calling loop. This file is small (≤ 100 lines) but it's where the product behaviour lives.

### 7.1 The Loop

```python
def handle_user_message(session_id: str, user_text: str) -> str:
    state = store.get(session_id)
    state.messages.append({"role": "user", "content": user_text})

    for hop in range(settings.max_tool_hops):
        messages = _build_messages(state)
        resp = llm_handler.chat(messages)

        if resp.kind == "message":
            state.messages.append({"role": "assistant", "content": resp.content})
            return resp.content

        # Tool calls path
        for call in resp.tool_calls:
            result = note_tools.execute(call.name, call.arguments)
            conversation_state.remember_referenced(state, result)
            state.messages.append({
                "role": "assistant",
                "tool_calls": [{"function": {"name": call.name,
                                             "arguments": call.arguments}}],
            })
            state.messages.append({
                "role": "tool",
                "name": call.name,
                "content": result.model_dump_json(),
            })

    # Safety net
    fallback = "I'm having trouble completing that — could you rephrase?"
    state.messages.append({"role": "assistant", "content": fallback})
    return fallback
```

### 7.2 `_build_messages`

Concatenates:
1. System prompt.
2. Ephemeral context line (last-referenced ids, pending confirmation).
3. The deque contents.

### 7.3 Pending confirmation handling

When a tool returns `needs_confirmation`, we set `state.pending_confirmation` and the *model* is responsible for asking the user. On the next user turn, if the text looks affirmative ("yes", "do it", "go ahead"), the model is nudged — via the context line — to call the same tool with `confirm=true`. We do not parse affirmation in Python ourselves; we surface context and let the LLM decide, which is the honest tool-calling pattern.

### 7.4 Definition of Done
- End-to-end CLI script: user types messages, full loop runs against real llama3.2, CRUD happens, delete requires "yes".

---

## Phase 9 — Evaluation Harness (`backend/eval/test_cases.py`)

**Goal:** run the 15 scenarios and emit a markdown report.

### 9.1 Scenario data model

```python
from dataclasses import dataclass
from typing import Callable

@dataclass
class Turn:
    user: str
    # One or more assertions on what the agent *did* this turn
    expect_tool: str | None = None           # e.g. "add_note"
    expect_args_contains: dict | None = None  # e.g. {"title": "standup"}
    expect_reply_contains: str | None = None  # substring match in final reply
    expect_no_tool: bool = False
    db_assertion: Callable[[], bool] | None = None

@dataclass
class Scenario:
    name: str
    turns: list[Turn]
    tags: list[str] = None   # e.g. ["happy","destructive","ambiguity"]
```

### 9.2 Runner shape

```python
def run_scenario(sc: Scenario) -> dict:
    reset_db()
    session_id = f"eval-{sc.name}"
    results = []
    for turn in sc.turns:
        with _capture_tool_calls() as captured:
            reply = intent_parser.handle_user_message(session_id, turn.user)
        results.append(_assert_turn(turn, reply, captured))
    return {"name": sc.name, "passed": all(r["ok"] for r in results), "turns": results}
```

`_capture_tool_calls` monkey-patches `note_tools.execute` to record each `(name, args)` tuple, then delegates.

### 9.3 Example scenario

```python
Scenario(
  name="07_multi_turn_reference",
  tags=["happy","multi_turn"],
  turns=[
    Turn(user="Save a note titled 'standup' — we moved it to Tuesdays. Tag it meetings.",
         expect_tool="add_note",
         expect_args_contains={"title": "standup", "tags": ["meetings"]}),
    Turn(user="Actually, also add that the new time is 10am.",
         expect_tool="update_note",
         expect_args_contains={"body": "10am"}),
  ],
)
```

### 9.4 Modes

- `python -m backend.eval.test_cases` → runs all scenarios against real Ollama, writes `eval/report.md`.
- `python -m backend.eval.test_cases --fast` → uses a **stub LLM** (`StubOllamaClient`) that returns scripted tool calls per scenario; good for CI.

### 9.5 Report format

```
# Eval Report — 2026-04-21 15:32
Pass rate: 13/15 (86.7%)

| # | Scenario                         | Result | Notes                          |
|---|----------------------------------|--------|--------------------------------|
| 1 | 01_add_simple_note               | PASS   |                                |
| 6 | 06_ambiguous_reference           | PASS   | Correctly asked which note     |
| 8 | 08_delete_with_confirmation      | PASS   |                                |
| 9 | 09_delete_declined               | PASS   | Note retained as expected      |
|12 | 12_contradiction_probe           | FAIL   | Model didn't flag conflict     |
```

### 9.6 Definition of Done
- `eval/report.md` exists with ≥ 13/15 passing on the real model.

---

## Phase 10 — Documentation & Submission

### 10.1 Artifacts

- `README.md` — quickstart, env, run, eval, troubleshooting.
- `docs/TOOLS.md` — one section per tool: signature, JSON schema, examples, error codes.
- `docs/DESIGN.md` — condensed version of this plan with lessons learned.
- `docs/PROMPTS.md` — the exact system prompt + rationale.

### 10.2 Submission polish
- Clean commit history grouped by phase.
- Tag `v1.0`.
- Include `eval/report.md` from the final run.

---

## Phase 11 (Bonus) — Containerisation

### 11.1 `Dockerfile` (backend)

```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend backend
COPY app app
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn","backend.main:app","--host","0.0.0.0","--port","8000"]
```

### 11.2 `docker-compose.yml`

```yaml
services:
  ollama:
    image: ollama/ollama:latest
    volumes: ["ollama:/root/.ollama"]
    ports: ["11434:11434"]
    healthcheck:
      test: ["CMD","ollama","list"]
      interval: 10s
      timeout: 5s
      retries: 5

  ollama-init:
    image: ollama/ollama:latest
    depends_on: { ollama: { condition: service_healthy } }
    entrypoint: ["/bin/sh","-c","ollama pull llama3.2"]
    environment: ["OLLAMA_HOST=http://ollama:11434"]

  app:
    build: .
    depends_on: { ollama-init: { condition: service_completed_successfully } }
    environment:
      OLLAMA_HOST: http://ollama:11434
      OLLAMA_MODEL: llama3.2
      DB_PATH: /data/notes.db
    volumes: ["notes:/data"]
    ports: ["8000:8000"]

volumes:
  ollama:
  notes:
```

### 11.3 Definition of Done
- `docker compose up` → browser at `localhost:8000` → fully working agent, no host dependencies besides Docker.

---

## Phase 12 (Bonus) — Semantic Search

### 12.1 What changes

- Add column `embedding BLOB` on `notes`.
- Generate embedding via Ollama's `nomic-embed-text` on `create_note` and `update_note`.
- Add `semantic=True` branch in `search_notes`:

```python
if semantic:
    q_vec = embed(query)
    rows = db.execute("SELECT id, title, body, embedding, updated_at FROM notes").fetchall()
    scored = [(cosine(q_vec, np.frombuffer(r["embedding"], dtype=np.float32)), r) for r in rows]
    scored.sort(reverse=True)
    top = [row_to_summary(r) for _, r in scored[:limit]]
```

### 12.2 Why `nomic-embed-text`
- Open, available via Ollama, 137M params, strong on retrieval benchmarks, no extra service.

### 12.3 Definition of Done
- "find notes related to project deadlines" returns a note titled "milestone list" even if neither word appears in it.

---

## Phase 13 (Bonus) — Multi-User Isolation

- Add `user_id TEXT NOT NULL` to `notes` and `tags`.
- Read `X-User-Id` header in `/chat` (stubbed auth, documented).
- Thread `user_id` through every service function (makes each query automatically scoped).
- Eval: add scenario proving user A cannot read user B's notes.

---

## Phase 14 (Bonus) — MCP Server

- New entrypoint `backend/mcp_server.py` using the official MCP Python SDK.
- Reuses `tools/schemas.py` and `tools/note_tools.py` directly — proving the tools are decoupled from transport.
- Registers each tool and points at `note_tools.execute`.
- Document `mcp run backend/mcp_server:server` and show an MCP-compatible client using it.

---

## Cross-Phase Summary Table

| Phase | Deliverable | Key Files | Grader signal it supports |
|---|---|---|---|
| 0 | Skeleton + Ollama health | `main.py`, `config.py` | Reproducibility |
| 1 | DB + FTS5 | `db/sqlite.py` | Persistence choice justification |
| 2 | Business logic | `services/note_service.py` | Clean decomposition |
| 3 | Tool schemas | `tools/schemas.py` | **Tool design (core grading)** |
| 4 | Dispatcher + gating | `tools/note_tools.py` | Destructive-confirm + ambiguity |
| 5 | LLM wrapper | `agent/llm_handler.py` | How we *use* the model |
| 6 | Session state | `agent/conversation_state.py` | Multi-turn awareness |
| 7 | Orchestrator | `agent/intent_parser.py` | Loop control + robustness |
| 9 | Eval harness | `eval/test_cases.py` | **Explicit requirement** |
| 10 | Docs | `README.md`, `docs/TOOLS.md` | Submission quality |
| 11–14 | Bonuses | Docker / semantic / multi-user / MCP | Upside |

