---
name: 03-note-agent
description: How the note-taking agent actually works — the seven tools, the two-step confirmation gate, the orchestrator loop, intent / commit / merge guardrails, and the per-tool flows end to end.
---

# 03 - Note Agent

The agent is the part of the system that turns a free-form user message into a correct sequence of typed tool calls against a real database. This document explains every mechanism that makes it behave: the tool surface, the orchestration loop, the guardrails that compensate for small-model quirks, and the full happy-path flow for each operation.

## Design Principles

These are the rules the code is built around. They are worth holding in mind while reading the rest of the document:

1. **One tool per user intent.** Seven small tools, each with a single responsibility. No mega-tool that "does everything note-related".
2. **Tool design is the #1 grading signal.** Typed arguments, a uniform result envelope, descriptions written for the model.
3. **Destructive actions are gated server-side.** The LLM cannot delete a note in one call — the tool itself requires two calls.
4. **Ambiguity is handled by asking, not guessing.** Search returns `candidates[]`; the system prompt forces a clarification question.
5. **The loop is bounded.** `MAX_TOOL_HOPS = 5` — never removed, never bypassed.
6. **The agent never fabricates.** The system prompt bans inventing ids, titles, tags, or success messages — all content in replies must come from a tool result in the conversation.

## The Seven Tools

```
┌─────────────────┬────────────────────────────────────────────────────────────┐
│ Tool            │ Purpose                                                    │
├─────────────────┼────────────────────────────────────────────────────────────┤
│ add_note        │ Create a new note (two-step: preview → commit)             │
│ list_notes      │ Recent notes, optionally filtered by tag and/or created_at │
│ list_tags       │ Top-N tags by usage count                                  │
│ search_notes    │ Semantic search (cosine similarity, threshold 0.35)        │
│ get_note        │ Fetch one note by integer id                               │
│ update_note     │ Patch an existing note (two-step: preview → commit)        │
│ delete_note     │ Delete by id (two-step: preview → commit)                  │
└─────────────────┴────────────────────────────────────────────────────────────┘
```

Every tool has a Pydantic argument model in `backend/tools/schemas.py` and a handler in `backend/tools/note_tools.py`. The dispatcher `execute(name, args)` is the single entry point the orchestrator calls.

See [02 - Data Models](./02-data-models.md) for the full Pydantic signatures.

## The Two-Step Confirmation Gate

Three tools (`add_note`, `update_note`, `delete_note`) take a `confirm: bool` argument that defaults to `false`. The gate is enforced **inside the tool handler** — the prompt alone is not trusted:

```python
# backend/tools/note_tools.py — add_note
def _add_note(raw: dict) -> ToolResult:
    args = AddNoteArgs.model_validate(raw)

    if not args.confirm:
        return ToolResult(
            ok=False,
            needs_confirmation=True,
            error_code="needs_confirmation",
            message="About to save this note — title '...', tag '...'. Show this preview to the user ...",
            data={"preview": {"title": args.title, "description": args.description, "tag": args.tag}},
        )

    note = note_service.create_note(args.title, args.description, args.tag)
    return ToolResult(ok=True, message=f"Created note #{note.id} ...", data=note.model_dump(mode="json"))
```

The first call with `confirm=false` touches nothing — it returns a preview and a sentinel (`needs_confirmation: true`). The second call with `confirm=true`, after explicit user agreement, actually writes. This pattern exists because:

- It is **defensible** — a malicious or buggy prompt cannot delete data in a single hop.
- It is **recoverable** — the UI shows a yellow "needs confirmation" card, the user can say no.
- It is **auditable** — every commit has a visible preview in the same session.

`needs_confirmation` is returned as `ok: false`. A preview is **not** a success. The system prompt is explicit: "`needs_confirmation: true` → this is a PREVIEW, NOT a save. NEVER write 'saved successfully' or invent a note id".

## Tool Dispatcher

```python
# backend/tools/note_tools.py
_HANDLERS: dict[str, Callable[[dict], ToolResult]] = {
    "add_note": _add_note,
    "list_notes": _list_notes,
    "list_tags": _list_tags,
    "search_notes": _search_notes,
    "get_note": _get_note,
    "update_note": _update_note,
    "delete_note": _delete_note,
}

def execute(name: str, raw_args: dict | None) -> ToolResult:
    """Run a tool and return a ToolResult. Never raises."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolResult(ok=False, error_code="invalid_arg", message=f"Unknown tool: {name!r}.")
    try:
        return handler(raw_args or {})
    except ValidationError as e:
        return ToolResult(ok=False, error_code="invalid_arg",
                          message=f"Invalid arguments for {name}: {e.errors(include_url=False)}")
    except Exception as e:
        logger.exception("Tool %s raised an unexpected exception", name)
        return ToolResult(ok=False, error_code="internal",
                          message=f"Internal error in {name}: {type(e).__name__}: {e}")
```

**Invariants** the dispatcher enforces:

| Case                              | Result                                                                  |
|-----------------------------------|-------------------------------------------------------------------------|
| Unknown tool name                 | `ok=false, error_code="invalid_arg"`                                    |
| `ValidationError` from Pydantic   | `ok=false, error_code="invalid_arg", message=<validation errors>`       |
| Any other `Exception`             | `ok=false, error_code="internal"` + `logger.exception()` for operators  |
| Happy path                        | Whatever the handler returns (always a `ToolResult`)                    |

The LLM cannot see a raw stack trace. It cannot see an unknown state. Every outcome is one of the shapes in [02 - Data Models](./02-data-models.md) — `ToolResult`.

## The Orchestrator Loop

The orchestrator lives in `backend/agent/intent_parser.py`. A single function drives one user turn.

```
handle_user_message(session_id, user_text, emit, model)
├── state = store.get(session_id)
├── state.messages.append({"role": "user", "content": user_text})
├── emit("user_echo", {...})
│
├── allow_tools   = looks_like_note_op(user_text) OR pending_confirmation
├── force_confirm = pending is add/update  AND  looks_like_commit_intent(user_text)
│
├── for _ in range(MAX_TOOL_HOPS):
│       messages = [system_prompt, (context), *state.messages]
│       resp = llm_handler.chat(messages, tools=... or [], on_delta=fwd)
│
│       if resp.kind == "message":
│           emit("assistant", {...}); emit("done", {}); return TurnResult
│
│       for call in resp.tool_calls:
│           emit("tool_call", {... status: running})
│           result = run_tool_call(state, call, force_confirm)
│             ├── sanitize_args        (drop empty strings)
│             ├── merge_with_pending   (rescue partial diffs)
│             ├── note_tools.execute(...)
│             ├── remember_referenced(state, result)
│             ├── set/clear pending_confirmation
│             └── append tool_call + tool result to state.messages
│           emit("tool_result", {...})
│
└── if loop exhausted:  emit fallback reply + done
```

Every emitted event is a frame in the SSE stream; see [01 - Architecture](./01-architecture.md) for the stream wiring.

### Why a Loop at All

Some user intents require two tool calls in sequence — for example:

1. User: *"change the meeting note to 7 pm"* → LLM calls `search_notes(query="meeting")` to find the id, then `update_note(note_id=...)` to make the edit.
2. User: *"delete that note"* (immediately after a preview) → LLM calls `delete_note(note_id=..., confirm=true)` then replies with a plain-text acknowledgement.

The loop runs the tool, puts the result in `state.messages`, and calls the model again until it emits a plain message. `MAX_TOOL_HOPS = 5` is the cap — enough for any realistic chain, tight enough to prevent a runaway.

## The Intent Gate

**Problem.** Small models (notably llama3.2 3B) cannot resist firing a tool when tools are present in the context, even when the user says "hi". The system prompt steers the reply text but not the decision to call.

**Fix.** Decide *per turn, before the LLM is called* whether tools should even be exposed. If the user's message doesn't look like a note operation, pass `tools=[]` to the provider — the model physically cannot fire a tool.

```python
_NOTE_KEYWORD_PATTERN = re.compile(
    r"\b("
    r"note|notes|notebook|jot|save|store|record|write|"
    r"add|create|remember|reminder|todo|task|"
    r"update|edit|change|modify|append|rename|"
    r"delete|remove|clear|drop|trash|"
    r"tag|tags|tagged|untag|category|"
    r"show|list|recent|find|search|look|lookup|get|fetch|open|read|summarize|recall|"
    r"meeting|appointment|schedule|agenda|calendar|"
    r"today|tomorrow|yesterday|tonight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"morning|afternoon|evening"
    r")\b",
    re.IGNORECASE,
)

def looks_like_note_op(text: str) -> bool:
    return bool(_NOTE_KEYWORD_PATTERN.search(text))
```

**Failure-mode tradeoffs.** The heuristic errs toward allowing tools ("false positive = one spurious tool call the prompt rules moderate"; "false negative = user has to rephrase"). A pending confirmation overrides the gate — mid-confirmation, tools must stay available so "yes" can trigger the commit.

```python
allow_tools = looks_like_note_op(user_text) or bool(state.pending_confirmation)
tools_for_turn = None if allow_tools else []   # None → use TOOL_DEFS, [] → no tools
```

## The Commit-Intent Gate

**Problem.** On compound commands like *"tag it development and save"*, larger models occasionally emit the tool call with `confirm=false` (they correctly apply the tag change but forget to commit the add). The user sees another preview card instead of a save.

**Fix.** Detect explicit commit words during a pending add/update confirmation and force `confirm=true` downstream regardless of what the model sent.

```python
_MERGEABLE_TOOLS = {"add_note", "update_note"}

_COMMIT_INTENT_PATTERN = re.compile(
    r"\b(save|saving|save\s+it|create|creating|create\s+it|commit|"
    r"confirm|confirmed|add\s+it|do\s+it|go\s+ahead|yes|yeah|yep|"
    r"ok(ay)?|sure)\b",
    re.IGNORECASE,
)

force_confirm = bool(
    state.pending_confirmation
    and state.pending_confirmation.get("tool") in _MERGEABLE_TOOLS
    and _looks_like_commit_intent(user_text)
)
```

The force is not magical — it only fires when **all three** conditions are true:

1. A confirmation is currently pending.
2. That pending confirmation is for an `add_note` / `update_note` (never for delete — delete is always an explicit two-step).
3. The user's message contains an affirmative commit word.

## Merge With Pending

**Problem.** On a modification turn during a pending add/update, small models send only the **diff** rather than the full argument set. A preview generated from `title=... description=... tag=null` followed by *"use tag development"* arrives as `title="" description="" tag="development"`. Without a merge step, the second call fails `min_length` validation on `title`.

**Fix.** Before dispatching a call that matches a pending confirmation's tool name, merge the new arguments onto the stored pending arguments. Empty strings and `None` do not clobber existing values.

```python
def _merge_with_pending(call: ToolCall, state: SessionState, *, force_confirm: bool = False) -> dict:
    """Merge new args onto a pending add/update preview; empty values don't clobber."""
    if call.name not in _MERGEABLE_TOOLS:
        return dict(call.arguments)
    pc = state.pending_confirmation
    if not pc or pc.get("tool") != call.name:
        return dict(call.arguments)

    merged: dict = dict(pc.get("args") or {})
    for key, value in call.arguments.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value
    merged["confirm"] = bool(call.arguments.get("confirm") or force_confirm)
    return merged
```

Combined with the commit-intent gate:

- *"use tag development"* → merge `tag="development"` onto pending add args, `confirm=false` → fresh preview.
- *"use tag development and save"* → merge tag **and** force `confirm=true` → commit in one call.

## Sanitize Args

Before the merge even runs, a pre-pass drops empty-string values so they behave as "field omitted" at the schema layer. Models sometimes emit `title: ""` to mean "I didn't compute a new value"; without sanitisation, the Pydantic model rejects them for failing `min_length=1`.

```python
def _sanitize_args(args: dict) -> dict:
    """Drop empty-string values so they act as 'field omitted' at the schema layer."""
    return {k: v for k, v in args.items() if not (isinstance(v, str) and not v.strip())}
```

## Auto-Sync on Updates

**Problem.** When a user says *"change it to 7 pm"* about a note whose title and description **both** mention `5 pm`, the LLM usually updates only one of the two fields — leaving an inconsistent state (`title: "Meeting at 7 pm"` but `description: "Meeting at 5 pm"`).

**Fix.** Before committing an update, diff the proposed fields against the current ones. If a numeric token (digits only, so `5 pm → 7 pm` counts but `coffee → tea` doesn't) changed in one field, propagate it into the other field **if** that other field also contains the old token.

```python
_DIGIT_RE = re.compile(r"\d")

def _extract_digit_substitutions(old: str, new: str) -> list[tuple[str, str]]:
    """Token-level replacements where at least one side contains a digit."""
    matcher = difflib.SequenceMatcher(None, old.split(), new.split())
    subs: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op != "replace":
            continue
        old_tok = " ".join(old.split()[i1:i2])
        new_tok = " ".join(new.split()[j1:j2])
        if _DIGIT_RE.search(old_tok) or _DIGIT_RE.search(new_tok):
            subs.append((old_tok, new_tok))
    return subs
```

The behaviour is intentionally conservative: only numeric edits propagate. A plain word swap like `coffee → tea` would false-positive on unrelated descriptions, so we do not attempt it.

**Worked example** — user edits a meeting note:

```
Current note #1:
  title       = "Meeting on Tuesday 5 pm"
  description = "Meeting with the dev team on Tuesday at 5 pm"

User says: "change it to 7 pm"
LLM emits: update_note(note_id=1, title="Meeting on Tuesday 7 pm", confirm=false)

auto_sync detects:
  • title changed: "5 pm" → "7 pm"  (has digit ✓)
  • description unchanged, contains "5 pm"
  → propagate into description

Preview shown to user:
  title       = "Meeting on Tuesday 7 pm"
  description = "Meeting with the dev team on Tuesday at 7 pm"
```

## Semantic Search

`search_notes` is cosine similarity against the note's stored embedding. The service lives in `backend/services/note_service.py`:

```python
def search_semantic(query: str, limit: int = 5,
                    threshold: float | None = None,
                    fallback_limit: int | None = None) -> tuple[list[NoteSummary], bool]:
    """Rank notes by cosine similarity; returns (results, above_threshold)."""
    if threshold is None:
        threshold = settings.search_threshold           # 0.35 by default
    if fallback_limit is None:
        fallback_limit = settings.search_fallback_limit # 3

    q_vec = embeddings.embed(query)                     # unit-norm float32
    with tx() as conn:
        rows = conn.execute(
            "SELECT id, title, description, tag, embedding, updated_at "
            "FROM notes WHERE embedding IS NOT NULL"
        ).fetchall()

    if not rows:
        return [], False

    scored = [(embeddings.cosine(q_vec, embeddings.from_blob(r["embedding"])), r) for r in rows]
    scored.sort(key=lambda x: -x[0])

    above = [t for t in scored if t[0] >= threshold]
    if above:
        return [_row_to_summary(r, similarity=s) for s, r in above[:limit]], True

    # Nothing cleared the bar — return the closest few anyway, flagged as low-confidence.
    return [_row_to_summary(r, similarity=s) for s, r in scored[:fallback_limit]], False
```

The dispatcher then shapes three distinct outcomes for the LLM:

| Case                                            | `ToolResult.message`                                                   |
|-------------------------------------------------|------------------------------------------------------------------------|
| Above threshold, one match                      | `"Found 1 matching note."`                                             |
| Above threshold, >1 matches                     | `"Found N matching note(s) — ask the user which one."` + `candidates[]` |
| Below threshold (fallback)                      | `"No strong match ... Here are the closest N note(s) as a best-effort fallback"` |
| Empty corpus                                    | `"No notes at all (or none have embeddings yet)."`                     |

The system prompt tells the LLM to distinguish these in its reply text: never claim a low-confidence fallback is a real match.

### Why Not FTS5?

Keyword matching breaks down on natural phrasings — `"the meeting note"`, `"my lunch one"`, typos. Semantic search via `nomic-embed-text` handles all of those with no extra indexes. Tag queries, which **are** exact-match, are served by a plain `WHERE tag = ?` with the partial index. No FTS needed.

## Per-Tool Flows

The following diagrams trace each tool end-to-end.

### `add_note` — Two-Step Happy Path

```
USER: "remember I need to call John about the contract"
   │
   ▼
LLM  (turn 1) emits:
  add_note(title="Call John re: contract",
           description="Remember to call John back about the contract",
           tag=None, confirm=false)
   │
   ▼
Tool handler sees confirm=false:
  returns ToolResult(ok=false, needs_confirmation=true,
                     data={"preview": {title, description, tag}})
   │
   ▼
LLM  reads the preview from ToolResult.data, replies:
  "I'll save this note:
     • Title: Call John re: contract
     • Description: Remember to call John back about the contract
     • Tag: none
   Confirm, modify, or cancel?"
   │
   ▼
USER: "yes save"
   │
   ▼
commit_intent_gate: YES  →  force_confirm=true
   │
   ▼
LLM  (turn 2) emits:
  add_note(confirm=true)                  ← small models often send just this
   │
   ▼
merge_with_pending:  add the stored title/description/tag from pending_confirmation
   dispatched args = { title, description, tag, confirm: true }
   │
   ▼
Tool handler sees confirm=true:
  note_service.create_note(...)
  pending_confirmation is cleared
  returns ToolResult(ok=true, message="Created note #42 ...",
                     data={id: 42, ...})
   │
   ▼
LLM replies: "Saved note #42."
```

### `update_note` — Reasoning About Which Fields to Change

```
USER: "change my 5pm meeting to 7pm"
   │
   ▼
LLM  (turn 1) emits:  search_notes(query="5pm meeting")
   │
   ▼
Tool handler → service: cosine over corpus → top match: id=1
   │
   ▼
LLM reads note #1: title="Meeting on Tuesday 5 pm",
                    description="Meeting with the dev team on Tuesday at 5 pm"
   │
   ▼
LLM  (turn 2) emits:
  update_note(note_id=1, title="Meeting on Tuesday 7 pm", confirm=false)
   │
   ▼
Tool handler:
  • auto_sync detects "5 pm" → "7 pm" in title, propagates into description
  • confirm=false → returns preview of merged fields with synced description
   │
   ▼
LLM replies with the 3-bullet preview, asks "Confirm?"
   │
   ▼
USER: "yes"
   │
   ▼
LLM  (turn 3) emits:  update_note(note_id=1, confirm=true)
   │
   ▼
merge_with_pending brings back title + description; service writes; ok=true.
```

### `delete_note` — Hard Two-Step

```
USER: "delete the lunch note"
   │
   ▼
LLM  (turn 1): search_notes(query="lunch")  →  ToolResult lists 1 match (id=7)
   │
   ▼
LLM  (turn 2): delete_note(note_id=7, confirm=false)
   │
   ▼
Tool handler:  note = get_note(7)
               ToolResult(ok=false, needs_confirmation=true,
                          data={"preview": {id: 7, title, description, tag}})
   │
   ▼
LLM replies: "Permanently delete this note? ..."
   │
   ▼
USER: "yes"
   │
   ▼
LLM  (turn 3): delete_note(note_id=7, confirm=true)  ← note: delete is NOT in
                                                       _MERGEABLE_TOOLS,
                                                       so commit-intent gate
                                                       does not force it.
                                                       The LLM must set it.
   │
   ▼
Tool handler deletes; returns ok=true.
```

**Why delete is not auto-forced.** Commit-intent forcing exists for `add`/`update` because the user is still actively shaping the note (and the model often forgets the flag on compound commands). Delete has no "shaping" phase — either the user says yes to the exact preview or they don't. Requiring the LLM to emit `confirm=true` explicitly on delete is a deliberate extra gate.

### `search_notes` — Three Outcomes

```
USER: "what did I write about the Q3 roadmap"
   │
   ▼
LLM  emits: search_notes(query="Q3 roadmap")
   │
   ▼
service.search_semantic:
  embed(query) → top scores:
     ┌─────────────────────────────────────────────────┐
     │  note 4  "Q3 planning notes"        sim=0.71 ✓  │  above threshold
     │  note 7  "Quarter targets"          sim=0.52 ✓  │  above threshold
     │  note 2  "Grocery list"             sim=0.08    │  below threshold
     └─────────────────────────────────────────────────┘
  above_threshold = True, 2 matches

dispatcher sees >1 above threshold → sets `candidates[]`
   │
   ▼
LLM sees ToolResult with candidates[], asks user to disambiguate:
  "I found 2 matching notes:
     • Title: Q3 planning notes ...
     • Title: Quarter targets ...
   Which one did you mean?"
```

### `list_notes` — The Simplest Flow

```
USER: "show my recent notes"
   │
   ▼
LLM emits:  list_notes(limit=10)
   │
   ▼
service:  SELECT ... ORDER BY updated_at DESC LIMIT 10
   │
   ▼
ToolResult(ok=true, data=[NoteSummary, NoteSummary, ...])
   │
   ▼
LLM formats: 3-bullet block per note (Title / Description / Tag), no ids.
```

`list_notes` also accepts **`date_from` / `date_to`** for temporal queries — "what did I write last week?" resolves to `list_notes(date_from=..., date_to=...)` using today's date from the `(context)` line. Tag and date filters compose; the service builds a dynamic `WHERE` over `created_at` on the same single-table index.

```
USER: "what notes did I write in the last 14 days?"
   │
   ▼
LLM computes relative dates from (context) → emits:
  list_notes(date_from="2026-04-08", date_to="2026-04-22")
   │
   ▼
service:  WHERE created_at >= ? AND created_at <= ?
          ORDER BY updated_at DESC LIMIT 10
   │
   ▼
Only notes in the window come back; the LLM renders them in the 3-bullet format.
```

### `list_tags` — Suggestion During Add Flow

When the user adds a note without specifying a tag, the prompt instructs the model to call `list_tags(4)` and offer the existing tags so the user can reuse rather than create. This keeps the tag vocabulary compact.

```
USER: "add a note: prep for tomorrow's demo"
   │
   ▼
LLM emits:  list_tags(limit=4)
   │
   ▼
service:  SELECT tag, COUNT(*) AS c FROM notes
          WHERE tag IS NOT NULL GROUP BY tag
          ORDER BY c DESC, tag ASC LIMIT 4
   │
   ▼
LLM may paraphrase:  "Your top tags are: work (12), personal (5), ideas (3), meeting (2).
                       Do you want one of these, a new tag, or no tag?"
```

## Error Handling, End-to-End

Every layer is responsible for the errors it owns:

```
┌──────────────────────────────────────────────────────────────────────────┐
│  service layer      raises ValueError / RuntimeError on impossible input │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  tool handler       catches ValueError → ToolResult(ok=false)            │
│                     returns ok=false / not_found / invalid_arg explicitly│
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  dispatcher         catches ValidationError → invalid_arg                │
│                     catches everything else → internal + log             │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  orchestrator       appends the ToolResult JSON to state.messages        │
│                     emits tool_result SSE event                          │
│                     loops back — the next LLM call sees the failure      │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  LLM                sees ok=false and explains to user in plain English  │
│                     (prompted to NEVER retry with the same bad args)     │
└──────────────────────────────────────────────────────────────────────────┘
```

### Orchestrator-Level Fallback

If the model exhausts `MAX_TOOL_HOPS` without ever emitting a plain message, the orchestrator returns a canned fallback rather than looping forever:

```python
_FALLBACK_REPLY = (
    "I'm having trouble completing that — could you rephrase or break it into smaller steps?"
)
```

This is also stored into the session history so subsequent turns see that this turn concluded.

### Empty-Content Safety Net

Sometimes a provider returns `kind="message"` with empty `content` — Gemini 2.5 Flash has been observed doing this on short tool-enabled prompts, for example. The orchestrator retries the LLM call once (without streaming, so no duplicate delta tokens), then falls back to a friendly nudge only if the retry also comes back empty:

```python
if resp.kind == "message" and not (resp.content or "").strip():
    logger.warning("Empty response from model=%s ... — retrying once", ...)
    resp = llm_handler.chat(messages, tools=tools_for_turn, on_delta=None, model=model)

if resp.kind == "message":
    reply = (resp.content or "").strip()
    if not reply:
        reply = "Sorry, I didn't quite catch that. Could you rephrase?"
```

If the retry returns tool calls, the loop's tool-call branch picks them up naturally — the retry isn't locked to the `message` path.

## Why It Works

Reasoning at the seams rather than trusting any single component end-to-end:

| Layer                      | What it is trusted for                                 |
|----------------------------|---------------------------------------------------------|
| **Prompt**                 | Style, flow, reply formatting, which tool for which intent |
| **Intent gate** (regex)    | Refusing to expose tools on obvious chit-chat           |
| **Commit-intent gate**     | Forcing `confirm=true` when the user explicitly says so |
| **Pending-merge**          | Recovering full args when the model sends only a diff   |
| **Tool handler gate**      | Refusing any write that didn't pass `confirm=true`      |
| **Pydantic validation**    | Refusing malformed or out-of-range arguments            |
| **Auto-sync**              | Keeping title and description consistent on numeric edits |
| **Bounded loop**           | Refusing to run forever                                 |

No single one of these is sufficient; together they are. The prompt alone cannot guarantee a gate; the gate alone cannot guarantee user-friendly behaviour. Each compensates for the failure modes of the layer below it.

## Related Docs

- [01 - Architecture](./01-architecture.md) — where these components sit and how they're wired.
- [02 - Data Models](./02-data-models.md) — the Pydantic tool arg shapes and the `ToolResult` envelope.
- [04 - Memory and State](./04-memory-and-state.md) — how `pending_confirmation` and `last_referenced_note_ids` are maintained across turns.
