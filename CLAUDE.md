# CLAUDE.md

Operating guide for Claude Code working in this repository. Read this first on every session.

---

## 0. One-Line Context

This is a **conversational note-taking agent** (TechLabs AI Engineer assessment). A Python backend talks to **llama3.2 via Ollama**, exposes note CRUD as typed tools, and a **Next.js frontend** renders a 70/30 chat-vs-tool-calls view.

**Source of truth for the full plan:** `docs/PLAN.md` (Part II, "Detailed Build Phases", starts at line 336).
**Frontend spec:** `docs/FRONTEND_PLAN.md`.
**Assessment brief:** `docs/TechLabs_AI_Engineer_Assessment.pdf`.

Before proposing any architectural change, re-read the relevant phase in `PLAN.md`. If your suggestion contradicts it, flag the contradiction explicitly instead of silently diverging.

---

## 1. Top-Level Repo Layout

```
Techlabs/
├── CLAUDE.md                  # this file
├── docs/                      # plans + assessment brief
│   ├── PLAN.md                # backend master plan (Part I + phases)
│   ├── FRONTEND_PLAN.md       # Next.js UI plan
│   └── TechLabs_AI_Engineer_Assessment.pdf
├── backend/                   # Python / FastAPI / Ollama
│   ├── main.py                # FastAPI entrypoint (Phase 0 / 8)
│   ├── config.py              # frozen Settings dataclass
│   ├── agent/                 # LLM loop, state, intent parsing
│   │   ├── llm_handler.py
│   │   ├── intent_parser.py
│   │   └── conversation_state.py
│   ├── tools/                 # schemas + dispatcher (LLM-facing)
│   │   ├── schemas.py
│   │   └── note_tools.py
│   ├── services/              # pure business logic (no HTTP, no LLM)
│   │   └── note_service.py
│   ├── db/                    # SQLite + FTS5
│   │   └── sqlite.py
│   └── eval/                  # evaluation harness
│       └── test_cases.py
└── app/                       # Next.js 15 frontend (App Router, TS, Tailwind)
```

**Invariant:** every file in the repo belongs to one of those directories. If you're tempted to create a new top-level folder, push back on the request or propose it as an explicit change.

---

## 2. Golden Architectural Rules

These come directly from the plan. Violations should be fixed, not tolerated.

1. **Layer discipline (backend):**
   - `agent/` talks to the LLM and owns dialogue state. Never touches the DB.
   - `tools/` validates args via Pydantic and dispatches. No business logic.
   - `services/` is pure note logic. Never imports from `ollama`, `fastapi`, or `agent/`.
   - `db/` is raw `sqlite3`. Nothing above it knows about SQL.
2. **Tool design is the #1 grading signal.** One tool per user intent. Typed args. Uniform `ToolResult` envelope. No mega-tools.
3. **Destructive actions need two steps.** `delete_note(confirm=False)` → preview + `needs_confirmation`; only the second call with `confirm=True` actually deletes. Enforced in the service, not trusted from the LLM.
4. **Ambiguity is handled by asking, not guessing.** Search returns `candidates[]` and the system prompt forces a clarification question.
5. **Tool-call loops are bounded.** `MAX_TOOL_HOPS=5` in the orchestrator. Never remove this guard.
6. **No ORM.** Raw SQL in `backend/db/sqlite.py`. The grader must be able to read every query.
7. **Local-only model.** llama3.2 via Ollama. No OpenAI/Anthropic calls get added without an explicit request.

---

## 3. Phase-Aware Development

Work phase-by-phase as defined in `docs/PLAN.md` Part II. Each phase has a "Definition of Done" section — **treat it as the acceptance test**.

| # | Phase | Key file(s) |
|---|---|---|
| 0 | Skeleton | `backend/main.py`, `backend/config.py` |
| 1 | DB + FTS5 | `backend/db/sqlite.py` |
| 2 | Note service | `backend/services/note_service.py` |
| 3 | Tool schemas | `backend/tools/schemas.py` |
| 4 | Dispatcher | `backend/tools/note_tools.py` |
| 5 | LLM handler | `backend/agent/llm_handler.py` |
| 6 | Session state | `backend/agent/conversation_state.py` |
| 7 | Orchestrator | `backend/agent/intent_parser.py` |
| 9 | Eval harness | `backend/eval/test_cases.py` |
| 10 | Docs | `README.md`, `docs/TOOLS.md` |
| 11+ | Bonuses | Docker, semantic search, multi-user, MCP |

Frontend phases F0–F5 are in `docs/FRONTEND_PLAN.md §11`. F0/F1 can run in parallel with backend P0–P2.

**Before starting a phase,** state which phase you're doing and quote its Definition of Done. **After finishing,** verify each DoD bullet and report which ones pass.

---

## 4. Commands Cheat Sheet

### Python (backend)

Run from repo root (not from inside `backend/`) so imports like `backend.config` resolve.

```bash
# First time
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Pull model once
ollama pull llama3.2

# Dev server
uvicorn backend.main:app --reload

# Health check
curl http://localhost:8000/health

# Unit tests
pytest backend/tests -q

# Eval harness (real Ollama)
python -m backend.eval.test_cases

# Eval harness (stubbed LLM, CI-safe)
python -m backend.eval.test_cases --fast

# Inspect DB
sqlite3 data/notes.db ".schema"
sqlite3 data/notes.db "SELECT id,title,updated_at FROM notes ORDER BY id DESC LIMIT 10;"
```

### Next.js (frontend)

```bash
cd app
npm install
npm run dev           # http://localhost:3000
npm run build && npm run start
npm run lint
npm run typecheck     # tsc --noEmit
```

### Ollama

```bash
ollama list                    # installed models
ollama ps                      # running models / memory
ollama pull nomic-embed-text   # bonus: semantic search
curl http://localhost:11434/api/tags
```

### Docker (Phase 11, bonus)

```bash
docker compose up --build      # brings up ollama + app end-to-end
docker compose logs -f app
```

---

## 5. Coding Conventions

### Python
- **Python 3.10**, type hints everywhere, `from __future__ import annotations` at the top (required — lets us use `X | Y` and `list[X]` while still staying 3.10-compatible).
- **Pydantic v2** for all external-facing data (tool args, tool results, API bodies). Use `model_json_schema()`, not hand-written JSON.
- **Dataclasses** for internal state (session state, settings).
- **No decorators-as-config** (skip FastAPI dependency magic unless clearly justified).
- **Errors:** raise inside services; catch at the boundary (dispatcher, HTTP layer). The LLM should never see a raw stack trace — it must get a `ToolResult` with `ok=false`.
- **Logging:** `logging.getLogger(__name__)`, never `print` in committed code.
- **Tests:** `pytest`, files under `backend/tests/test_*.py`. Use `sqlite3.connect(":memory:")` or a temp DB, never the real `data/notes.db`.
- **Formatting:** Ruff default config is fine. Run `ruff format .` before committing.

### TypeScript (frontend)
- **Strict mode on** in `tsconfig.json`.
- **No `any`.** Use `unknown` + narrow, or define the type.
- Components are **function components**, default-exported, named in PascalCase, one per file.
- Shared types live in `app/src/app/types.ts`.
- Tailwind utilities only; no CSS modules, no inline `style` props unless dynamic.
- Client components opt in with `"use client"` at the top; everything else stays server-first.
- Use `crypto.randomUUID()` for ids (browser-native, zero deps).

### Comments
- **Explain intent, not mechanics.** A comment saying `// increment counter` above `i++` is noise.
- Good comment example: `// We retry once here because llama3.2 3B occasionally emits tool calls as text JSON.`
- Every non-obvious design choice should have a one-line rationale near the code.

---

## 6. LLM & Tool-Calling Rules

These keep the agent well-behaved and the code interview-defensible.

1. **System prompt lives in one file** (`backend/agent/llm_handler.py` or a sibling `prompts.py`). Do not inline prompts elsewhere.
2. **Tool definitions come from `backend/tools/schemas.py`** via `TOOL_DEFS`. Never hand-write an Ollama tool descriptor at a call site.
3. **Every tool returns a `ToolResult`** (`ok, message, data?, needs_confirmation?, candidates?, error_code?`). Never return a bare dict to the LLM.
4. **Temperature stays low** (default 0.2). Only change with justification.
5. **JSON-repair fallback in `llm_handler`** is the only place we tolerate loose parsing. Don't scatter JSON repair across the codebase.
6. **`conversation_state` owns "that note" / "the last one."** Orchestrator injects a hidden context line before each user turn listing `last_referenced_note_ids`.

---

## 7. Things You Must Not Do

- ❌ Call the DB from `agent/` or from the HTTP layer directly.
- ❌ Add an ORM (SQLAlchemy, Prisma, etc.).
- ❌ Swap the LLM provider without being asked.
- ❌ Delete the `MAX_TOOL_HOPS` guard.
- ❌ Trust the LLM to gate destructive actions on its own. Gate in the service.
- ❌ Store API keys or secrets in code. Use `.env` (and keep it gitignored).
- ❌ Commit `data/notes.db`, `.venv/`, `node_modules/`, or `.env`.
- ❌ Introduce a state library (Redux/Zustand) in the frontend. `useReducer` is enough.
- ❌ Create new top-level directories without asking.
- ❌ Write narrating comments (`// import the module`, `// loop over items`).

---

## 8. Things You Should Do Proactively

- ✅ Re-read `docs/PLAN.md` before a new phase.
- ✅ Run lints after edits (ReadLints) and fix what you introduced.
- ✅ Add a small `pytest` alongside new business logic in `services/` or `tools/`.
- ✅ Keep `ToolResult` schemas in sync when changing a tool.
- ✅ Regenerate `eval/report.md` after behavior-affecting changes.
- ✅ Update `docs/TOOLS.md` whenever a tool's signature changes.
- ✅ Mention the phase number in commit messages: `P3: add SearchNotesArgs`.

---

## 9. Working Style Inside Cursor / Claude Code

- **Plan before you code on anything ≥ 3 steps.** Use the TodoWrite tool.
- **Cite files with `path/to/file.py`** in prose so the user can click through.
- **Keep tool calls batched** when independent (reads, searches) — do not serialize them.
- **Never run destructive shell commands** (`rm -rf`, `git reset --hard`, `drop table`) without explicit user confirmation.
- **If you hit a tradeoff you can't resolve alone, ask** with a compact `AskQuestion` — don't silently pick.
- **When you finish a phase**, summarize: (a) what changed, (b) which DoD bullets pass, (c) what's next.

---

## 10. Current Repo State (Snapshot)

As of the last PLAN update, these exist:

- `backend/db/sqlite.py` — implemented (Phase 1 looks done; verify DoD before moving on).
- `backend/db/__init__.py` — empty package marker.
- `backend/config.py` — referenced by `sqlite.py`; confirm it exists before assuming it does.
- Everything else in `backend/` is scaffolded as empty directories.
- `app/` is an empty directory awaiting Next.js scaffold (frontend F0).
- No `requirements.txt`, no `package.json`, no `.env.example` committed yet — these are early-phase deliverables to add.

**First thing to do on any fresh session:** run `ls backend app` and `head -5 backend/config.py` (if it exists) to ground yourself in what's actually present, rather than what the plan says.

---

## 11. Quick Map: "I want to change X, which file?"

| Goal | Edit |
|---|---|
| Add/rename a tool | `backend/tools/schemas.py` (+ handler in `note_tools.py`, + service method) |
| Change how the agent remembers context | `backend/agent/conversation_state.py` |
| Tighten or loosen the system prompt | `backend/agent/llm_handler.py` |
| Add a new search filter | `backend/services/note_service.py` + `SearchNotesArgs` |
| Change DB schema | `backend/db/sqlite.py` (update `SCHEMA_SQL` and add migration note) |
| Add an eval scenario | `backend/eval/test_cases.py` |
| Change the chat UI layout | `app/src/app/page.tsx` |
| Add a visual state to a tool card | `app/src/app/components/ToolCallCard.tsx` + `StatusBadge.tsx` |
| Wire a new SSE event from backend to UI | `backend/main.py` streaming handler + `app/src/app/lib/api.ts` + reducer |

---

## 12. Success Definition (the whole project)

1. `docker compose up` (or two terminals with `uvicorn` + `next dev`) brings up a working chat agent end-to-end.
2. A user can do full CRUD + reasoning on notes via natural language through the UI.
3. Destructive actions require explicit confirmation, and the eval harness proves it.
4. `eval/report.md` shows ≥ 13/15 scenarios passing on real llama3.2.
5. Tool schemas and design decisions are clean enough that every line is defensible in an interview.

Keep that bar. When in doubt, simplify.
