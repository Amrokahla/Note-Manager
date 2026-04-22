# Note Agent

A conversational note-taking agent. The LLM calls typed tools to add, search, edit, and delete notes in a local SQLite database, and a Next.js UI renders the chat alongside every tool call the agent makes.

## Stack

- **Backend** — FastAPI + Pydantic v2, raw SQLite, streaming `/chat` via SSE.
- **Agent** — pluggable LLM providers (Ollama for local `llama3.x`, Google Gemini 2.5 Pro / Flash). Seven tools, two-step confirmation for add / update / delete, semantic search via `nomic-embed-text`, and a date-range filter on `list_notes`.
- **Frontend** — Next.js 16 (App Router), React 19, Tailwind v4. 70/30 chat-vs-tool-calls layout.

## Quick start — Docker (recommended)

The shortest path. One command, no host dependencies beyond Docker itself:

```bash
docker compose up --build
```

This brings up four services:

1. `ollama` — the model daemon, port `11434`.
2. `ollama-init` — runs once on first boot, pulls **only** `nomic-embed-text` (~274 MB) for semantic search, then exits. No chat model is pulled by default.
3. `backend` — FastAPI on port `8000`, waits for `ollama-init` to finish.
4. `frontend` — Next.js on port `3000`.

Open <http://localhost:3000> once the backend logs `Application startup complete`. Notes persist in a named volume (`notes`) across restarts.

### Chat model — Gemini (recommended) or local Ollama (opt-in)

**Gemini** is the default for chat. Drop your key into `.env`:

```bash
cp .env.example .env
# edit GEMINI_API_KEY= with your key from https://aistudio.google.com/app/apikey
docker compose up --build
```

Leave `GEMINI_API_KEY` unset and the UI will still let you pick Gemini, but requests will fail with a clear "GEMINI_API_KEY is not set" error.

**Local Ollama chat** is opt-in — set `OLLAMA_CHAT_MODEL` in `.env` and the init container will pull it on first boot:

```bash
# In .env:
OLLAMA_CHAT_MODEL=llama3.2:1b    # ~1.3 GB, fastest to download
# OLLAMA_CHAT_MODEL=llama3.2     # ~2 GB, better tool-calling
# OLLAMA_CHAT_MODEL=qwen2.5:3b   # ~2 GB, alternative
```

The backend's default `OLLAMA_MODEL` tracks whatever you set here. Leave it empty to save the download time (and disk) if you're only using Gemini.

First-boot cold start is a few minutes while Ollama pulls the embedding model (and optionally the chat model). Subsequent starts are seconds (`docker compose up` without `--build`).

## Environment

The defaults work for Docker out of the box. For local runs (next section), copy and edit:

```bash
cp .env.example .env
```

| Variable | Default | Notes |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama daemon URL |
| `OLLAMA_MODEL` | `llama3.1` | Default local chat model |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model for search |
| `DB_PATH` | `./data/notes.db` | SQLite file path (auto-created) |
| `MAX_TOOL_HOPS` | `5` | Per-turn tool-call limit |
| `HISTORY_TURNS` | `20` | Messages retained per session |
| `SEARCH_THRESHOLD` | `0.35` | Cosine similarity cutoff for matches |
| `SEARCH_FALLBACK_LIMIT` | `3` | Closest-N returned when no match beats the threshold |
| `GEMINI_API_KEY` | _(empty)_ | Required only if you pick a Gemini model in the UI |

## Alternative — run without Docker

Use this path if you already have Ollama installed and prefer two local dev servers with hot reload.

### Prerequisites

- Python 3.10+
- Node.js 20+ (Next.js 16 requirement)
- [Ollama](https://ollama.com) if you want to run locally without an API key
- A Google Gemini API key if you want to use Gemini (free tier works for Flash)

### Setup — Ollama (local, no API key)

Install Ollama, then pull the models the agent uses:

```bash
ollama pull llama3.2            # chat / tool-calling model
ollama pull nomic-embed-text    # required for semantic search
```

`nomic-embed-text` is not optional — `search_notes` relies on it, and startup runs a backfill over any un-embedded rows.

Verify Ollama is reachable:

```bash
curl http://localhost:11434/api/tags
```

### Setup — Gemini

Create an API key at <https://aistudio.google.com/app/apikey>, then put it in `.env`. `gemini-2.5-flash` is the default in the UI and works on the free tier; `gemini-2.5-pro` usually requires a paid tier.

You can run the app with Gemini only — but `search_notes` still needs Ollama's `nomic-embed-text` running locally for embeddings. If you don't want Ollama at all, don't use semantic search.

### Run

Two terminals — backend on `:8000`, frontend on `:3000`.

**Backend** (from repo root):

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

Health check: `curl http://localhost:8000/health`.

**Frontend** (in another terminal):

```bash
cd app
npm install
npm run dev
```

Open <http://localhost:3000>. Pick your model from the header dropdown and start talking to your notes.

## Evaluation Harness

With the backend running (Docker or local), execute the 14 scripted scenarios from `backend/eval/test_cases.py`:

```bash
source venv/bin/activate
python -m backend.eval.test_cases                # all scenarios
python -m backend.eval.test_cases --only 07 14   # subset by name prefix
```

Each scenario clears the `notes` table, optionally seeds rows (with backdated `created_at` where needed), replays a short conversation against the live agent, and asserts the tool sequence + final status + any DB conditions. One plan scenario (#12, contradiction probe) stays skipped — its assertion is subjective. After each run the harness writes a markdown summary to `backend/eval/report.md`.

## Documentation

- [`docs/DESIGN.md`](docs/DESIGN.md) — one-pager: every key decision with a one-sentence rationale.
- [`docs/01-architecture.md`](docs/01-architecture.md) — system overview + data flow + streaming pipeline.
- [`docs/02-data-models.md`](docs/02-data-models.md) — Pydantic models, provider mappers, SQLite schema.
- [`docs/03-note-agent.md`](docs/03-note-agent.md) — tools, confirmation gates, orchestrator flows.
- [`docs/04-memory-and-state.md`](docs/04-memory-and-state.md) — session store, context line, reducer state.
- [`docs/TOOLS.md`](docs/TOOLS.md) — per-tool reference (args, returns, error codes, examples).
- [`backend/eval/report.md`](backend/eval/report.md) — latest pass/fail run (14/14 scenarios).
- [`docs/files/`](docs/files/) — file-by-file walkthroughs (local reference; gitignored).
