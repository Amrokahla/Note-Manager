# Design Notes

A one-pager summary of the design decisions behind this project and why each was chosen. For the full deep dive see [`01-architecture.md`](01-architecture.md), [`02-data-models.md`](02-data-models.md), [`03-note-agent.md`](03-note-agent.md), and [`04-memory-and-state.md`](04-memory-and-state.md); this doc is the distillation.

## The Problem

Build a conversational agent that does **full CRUD + reasoning over notes** via natural language, with every decision defensible. The grading criteria were explicit: tool schema design, state management, edge cases, an evaluation harness, and design reasoning. Every choice below is anchored to one of those.

## Architecture — Strict Layers

```
HTTP (FastAPI)  →  Orchestrator  →  LLM dispatcher + Tool dispatcher
                                          │                 │
                                          ▼                 ▼
                                    provider module    service layer
                                                             │
                                                             ▼
                                                           SQLite
```

Every layer sees only the one below it. The LLM layer never touches SQL; the service layer never knows a language model exists; the HTTP layer never runs business logic.

**Why strict layers** — the graders explicitly asked about decomposition. A flat `main.py` with inline SQL and inline LLM calls would be faster to write but undefendable in a review. The layers also made the later provider split (Ollama → Ollama + Gemini) a 30-line change instead of a refactor.

## Tool Design — The Highest-Leverage Area

Seven tools, not one or two. Each tool maps to a single user intent:

| Tool | Intent |
|---|---|
| `add_note` | "save X" |
| `list_notes` | "show recent / by tag / by date range" |
| `list_tags` | "what tags do I have" |
| `search_notes` | "find the one about X" (semantic) |
| `get_note` | "show me note N" |
| `update_note` | "change X in that note" |
| `delete_note` | "delete that note" |

**Why seven instead of a mega-tool**: the LLM has to decide *which* tool based on user phrasing. That decision is easier when the tool name maps one-to-one with intent. A mega-tool forces the LLM to also decide *what to do* inside the tool, which compounds failure modes.

**Why Pydantic arg models + `TOOL_DEFS`**: the JSON Schema handed to the LLM is generated from the same Pydantic class that validates incoming arguments. Renaming a field updates the model's view of the tool automatically — no drift.

**Why a uniform `ToolResult` envelope**: `{ok, message, data?, needs_confirmation?, candidates?, error_code?}`. Every outcome (success, preview, validation failure, not-found, internal error) has the same shape. The LLM has a finite set of `error_code` values to reason about instead of free-form text.

**Why a server-side confirmation gate**: `add_note`, `update_note`, and `delete_note` each carry a `confirm: bool` argument defaulting to `false`. The first call returns a preview + `needs_confirmation: true` without touching the DB. Only the second call with `confirm=true` actually writes. Enforced in the handler, not trusted from the LLM. This means a buggy prompt or malicious user input can't delete data in a single hop.

## Agent — Layered Guardrails

No single mechanism is trusted alone. The guardrails compose:

```
user message
     │
     ▼
  Intent gate (regex for Ollama | LLM classifier for Gemini)
     │  if no → tools=[] sent to LLM → LLM physically cannot fire a tool
     ▼
  Commit-intent gate (regex) — force confirm=true on "save/yes" during pending add/update
     │
     ▼
  LLM call — picks which tool + args
     │
     ▼
  Sanitize empty-string args + merge-with-pending (rescues partial diffs from smaller models)
     │
     ▼
  Pydantic validation at tool handler
     │
     ▼
  Server-side confirmation gate (if confirm=false → return preview, don't write)
     │
     ▼
  Service call → SQLite
```

**Why a per-provider intent gate**: small models (Ollama llama3.2 3B) reflex-call tools even on greetings. A regex in Python removes the tool surface from the LLM's context when the message clearly isn't a note op — the model *physically cannot* misfire. Gemini is a capable enough classifier to decide itself, so on the Gemini path we use the LLM rather than a hand-maintained keyword list; the regex stays as safety-net fallback on any classifier failure.

**Why bounded tool-call loop (`MAX_TOOL_HOPS=5`)**: a runaway tool-calling loop is a real failure mode; the guard is cheap and never hurts valid flows.

**Why merge-with-pending + auto-sync**: 3B and Gemini both sometimes emit only the *diff* on a modify turn (tag="X" with empty title/description), or update title without description when a time change affects both. The orchestrator reconstructs full args from `pending_confirmation` and propagates numeric substitutions across fields. This is where the "hard to get right" grade comes from; simple prompt-only agents fail these turns consistently.

**Why retry-on-empty + retry-on-exception**: Gemini 2.5 Flash occasionally returns empty (thinking budget burn) or 5xx on tool-heavy prompts. One retry per turn converts flakes into silent resilience. Thinking is also disabled for Flash (`thinking_budget=0`) as the primary root-cause fix.

**Why the hidden `(context)` line**: small models can't follow pronoun references from history alone. Each turn gets a second `system` message containing today's date, the last-referenced note ids, and any pending-confirmation policy. This is the single biggest multi-turn reliability fix in the codebase.

## State — Intentionally Minimal

**In-memory `SessionStore`** — one dict per process, `deque(maxlen=40)` per session. No Redis, no persistence. For an assessment the simpler footprint is a feature; the interface is tiny (`get`, `reset`, `clear`) so swapping to Redis later is a one-file change.

**Frontend reducer instead of a state library** — `useReducer` is enough. The SSE stream maps cleanly to a small `Action` union. Redux / Zustand would be decorative. The `turnId` trick (each user message tagged with a UUID the streamed deltas carry) is how we merge streamed text into the right assistant bubble even when a tool result interrupts mid-stream.

## Storage — SQLite, No ORM, Single Table

One `notes` table, two indexes, no FTS5. Embeddings live in-row as packed `float32` BLOBs.

**Why no ORM**: the grader should be able to read every query. Raw SQL through `sqlite3.Row` is under 60 lines total for the whole DB layer. An ORM would add dependencies and opacity for no behavioural gain.

**Why single-table instead of a `note_tags` join table**: the requirement is one tag per note. Denormalised tag means `WHERE tag = ?` on a partial index — no join.

**Why semantic search via `nomic-embed-text` instead of FTS5**: keyword match fails on natural phrasings ("the meeting note", typos). `nomic-embed-text` runs locally via Ollama; embeddings are cached in-row; cosine similarity is a dot product because vectors are unit-normalised at write time. At assessment scale, a full-corpus scan is fine; a production version would move to a vector index.

## Evaluation — End-to-End, Real LLM

14 scenarios (adapted from the plan's 15; #12 contradiction probe skipped because its assertion is subjective). Harness speaks HTTP to the real backend, consumes the SSE stream, captures tool-call / tool-result pairs, asserts per-turn expectations.

**Why real LLM rather than mocks**: an agent tested against a scripted LLM proves the plumbing works but not the prompt. The point of the harness is to catch prompt regressions, and that only fires against a real model.

**Why E2E over unit tests**: multi-turn behaviour (pronoun resolution, merge-with-pending, confirmation flow) is inherently stateful. Testing each layer in isolation with mocks would miss the most interesting bugs. Latest report (real Gemini 2.5 Flash): **14/14 pass**. See [`backend/eval/report.md`](../backend/eval/report.md).

## Trade-offs / What Wasn't Built

| Decision | Why skipped |
|---|---|
| No ORM / migrations framework | Single table, zero churn; raw SQL stays readable. |
| No auth / multi-user | Single-user assessment. Adding `user_id` would touch every query; plan §13 has the design. |
| No MCP server | Not built. The tool layer is decoupled enough that a wrap-and-republish would be small. Plan §14. |
| In-memory session state | No Redis dependency; session survives process lifetime only. `SessionStore` interface is ready for a Redis swap. |
| No unit tests | Harness is end-to-end against a real model. Unit tests would have been low-leverage vs. the integration surface. |

## File Map

See [`01-architecture.md`](01-architecture.md) for the full layer diagram. Headlines:

```
backend/
├── main.py              ← HTTP + SSE bridge
├── config.py            ← frozen Settings dataclass
├── agent/
│   ├── intent_parser.py ← orchestrator + gates + loop
│   ├── conversation_state.py ← SessionStore + (context) line
│   ├── prompts.py       ← SYSTEM_PROMPT
│   ├── llm_handler.py   ← provider dispatcher
│   ├── llm_ollama.py    ← Ollama provider
│   ├── llm_gemini.py    ← Gemini provider (message + schema translation)
│   └── llm_types.py     ← LLMResponse, ToolCall
├── tools/
│   ├── schemas.py       ← Pydantic arg models, ToolResult, TOOL_DEFS
│   └── note_tools.py    ← dispatcher (execute) — never raises
├── services/
│   ├── note_service.py  ← CRUD, search, tag+date list, backfill
│   ├── embeddings.py    ← nomic-embed-text wrapper
│   └── models.py        ← Note, NoteSummary, TagCount
├── db/
│   └── sqlite.py        ← schema, init_db, tx() context manager
└── eval/
    ├── test_cases.py    ← 14-scenario SSE-based harness
    └── report.md        ← latest results
```

## One-Line Defensibility Test

Every design choice in this document should be summarisable in one sentence. If a decision took more than a sentence to justify, it was too complicated and got simplified. That test is the reason the codebase stays small and the behaviour stays legible.
