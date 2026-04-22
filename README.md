# Note Agent

A conversational note-taking agent. The LLM calls typed tools to add, search, edit, and delete notes in a local SQLite database, and a Next.js UI renders the chat alongside every tool call the agent makes.

## Stack

- **Backend** — FastAPI + Pydantic v2, raw SQLite, streaming `/chat` via SSE.
- **Agent** — pluggable LLM providers (Ollama for local `llama3.x`, Google Gemini 2.5 Pro / Flash). Seven tools, two-step confirmation for add / update / delete, semantic search via `nomic-embed-text`, and a date-range filter on `list_notes`.
- **Frontend** — Next.js 16 (App Router), React 19, Tailwind v4. 70/30 chat-vs-tool-calls layout.

## Prerequisites

- Python 3.10+
- Node.js 20+ (Next.js 16 requirement)
- [Ollama](https://ollama.com) if you want to run locally without an API key
- A Google Gemini API key if you want to use Gemini (free tier works for Flash)

## Setup — Ollama (local, no API key)

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

## Setup — Gemini

Create an API key at <https://aistudio.google.com/app/apikey>, then put it in `.env` (see next section). `gemini-2.5-flash` is the default in the UI and works on the free tier; `gemini-2.5-pro` usually requires a paid tier.

You can run the app with Gemini only — but `search_notes` still needs Ollama's `nomic-embed-text` running locally for embeddings. If you don't want Ollama at all, don't use semantic search.

## Environment

Copy the template and fill in what you need:

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
| `GEMINI_API_KEY` | _(empty)_ | Required only if you pick a Gemini model in the UI |

## Run

Two processes — backend on `:8000`, frontend on `:3000`.

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

With the backend running, execute the 14 scripted scenarios from `backend/eval/test_cases.py`:

```bash
source venv/bin/activate
python -m backend.eval.test_cases                # all scenarios
python -m backend.eval.test_cases --only 07 14   # subset by name prefix
```

Each scenario clears the `notes` table, optionally seeds rows (with backdated `created_at` where needed), replays a short conversation against the live agent, and asserts the tool sequence + final status + any DB conditions. One plan scenario (#12, contradiction probe) stays skipped — its assertion is subjective.
