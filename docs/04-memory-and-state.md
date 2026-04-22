---
name: 04-memory-and-state
description: How the agent remembers things — the per-session store, the rolling message deque, the hidden (context) line injection, pronoun resolution via `last_referenced_note_ids`, the pending-confirmation state machine, and the frontend session / reducer model.
---

# 04 - Memory and State

The agent has two memory surfaces. On the **backend** there is per-session state in memory: a rolling window of messages, a few small derived fields, and a "pending confirmation" slot. On the **frontend** there is reducer state: the client's sessionId, the message list, the tool-call cards, streaming flags.

Neither side writes chat to disk. The SQLite DB holds the notes themselves — not the conversations. Restart the backend and every session is gone; this is intentional and keeps the footprint small for the assessment.

This document explains exactly what is remembered, how it's maintained, and why each piece exists.

## Two Layers of Memory

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          BACKEND — in memory                              │
├──────────────────────────────────────────────────────────────────────────┤
│   SessionStore  (singleton, process-local)                                │
│     └── { session_id → SessionState }                                     │
│                                                                           │
│   SessionState                                                            │
│     ├── session_id                                                        │
│     ├── messages              deque[dict]  (maxlen = HISTORY_TURNS * 2)   │
│     ├── last_referenced_note_ids   list[int]  (pronoun resolution)       │
│     └── pending_confirmation       dict | None (two-step gate state)      │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                       FRONTEND — React useReducer                         │
├──────────────────────────────────────────────────────────────────────────┤
│   AppState                                                                │
│     ├── sessionId                  (client-generated UUID)                │
│     ├── messages                   ChatMessage[]  (user + assistant)      │
│     ├── toolCalls                  ToolCallRecord[]  (tool panel)         │
│     ├── isStreaming                boolean                                 │
│     ├── model                      ModelId  (persisted to localStorage)   │
│     └── error                      string | undefined                      │
└──────────────────────────────────────────────────────────────────────────┘
```

## Backend — `SessionStore`

```python
# backend/agent/conversation_state.py

class SessionStore:
    """In-memory per-session state; swap to Redis by reimplementing `get`/`reset`."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get(self, session_id: str) -> SessionState:
        existing = self._sessions.get(session_id)
        if existing is None:
            existing = SessionState(session_id=session_id)
            self._sessions[session_id] = existing
        return existing

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def clear(self) -> None:
        self._sessions.clear()
```

A plain dict. The orchestrator calls `store.get(session_id)` at the start of every turn — the first call for a new id creates an empty `SessionState` lazily. The interface is intentionally tiny (`get`, `reset`, `clear`) so swapping to Redis later is a one-file change.

The store itself is a module-level singleton:

```python
# backend/agent/intent_parser.py
store: SessionStore = SessionStore()
```

One process, one store. Good enough for the assessment; naive beyond that point. In production you'd move it behind a cache with per-session TTL and a durable fallback.

## `SessionState`

```python
@dataclass
class SessionState:
    session_id: str
    messages: deque[dict] = field(
        default_factory=lambda: deque(maxlen=_MAX_MESSAGES)
    )
    last_referenced_note_ids: list[int] = field(default_factory=list)
    pending_confirmation: dict | None = None
```

Four fields. Three of them are central to behaviour; the session id itself is just the handle.

### The Rolling Message Deque

```python
_MAX_MESSAGES = settings.history_turns * 2
```

`HISTORY_TURNS = 20` by default, so the deque holds up to 40 messages. A "turn" is one user message + one assistant/tool response — hence the `* 2`. When the deque fills, the oldest messages drop off automatically (Python `deque(maxlen=...)` is a ring buffer).

The trade-off behind the number:

| Value            | Effect                                                                 |
|------------------|------------------------------------------------------------------------|
| Too small (< 10) | The model loses multi-turn awareness — "that note" fails more often.   |
| Default (40)     | Covers any realistic chat without risking context-window overruns.     |
| Too large (> 80) | Llama 3 3B/8B's effective context degrades and response quality drops. |

The deque stores the **already-translated** canonical message shape (`{"role": ..., "content": ...}`), not provider-specific formats. The provider mappers only run at the moment of sending; see [02 - Data Models](./02-data-models.md).

### What Gets Appended

Every user turn writes three kinds of entries to the deque:

1. **User message** — appended once at the top of `handle_user_message`.
2. **Assistant tool call** — appended for every tool call the model emits, with the *merged* arguments (after `merge_with_pending`), not the raw arguments the model sent.
3. **Tool result** — appended as `{"role": "tool", "name": ..., "content": "<JSON of ToolResult>"}` after each dispatch.
4. **Assistant text** — appended once at the end of the turn when the model emits `kind="message"`.

Why merged args, not raw: the next hop (and future turns) need to see the **actual** dispatched call so that the trace is faithful. Storing the raw model output would confuse the model on later turns when it tries to reference what it "said".

### `last_referenced_note_ids` — Pronoun Resolution

```python
def remember_referenced(state: SessionState, result: ToolResult) -> None:
    """Harvest note ids from a ToolResult to resolve later pronoun references."""
    ids = _harvest_ids(result.data)
    if ids:
        state.last_referenced_note_ids = ids
```

After every tool call, `_harvest_ids` walks the result's `data` field and pulls out any `id: int`. Three shapes are supported:

| `data` shape                              | What is harvested              | Example source           |
|-------------------------------------------|--------------------------------|--------------------------|
| `[{"id": ..., ...}, ...]` (list of rows)  | every `id`                     | `list_notes`, `search_notes` |
| `{"id": ..., ...}` (single row)           | the one `id`                   | `get_note`, `create_note`, `update_note` |
| `{"preview": {"id": ..., ...}}`           | the `id` inside `preview`      | `delete_note` (needs_confirmation) |

The result is deduplicated while preserving order — a search that returns `[3, 5, 3]` collapses to `[3, 5]` so "that note" is never ambiguous between duplicates.

**`last_referenced_note_ids` is only updated when the result actually carries ids.** A failed search (empty `data=[]`) or an error does not wipe the previous pronoun targets — the user is probably still talking about whatever they last looked at.

### Why Pronoun Resolution Isn't Left to the Model

Llama 3 at 3B tracks pronoun references poorly from message history alone. It sees "the meeting note" and searches again, finds multiple, picks wrong. Explicitly listing the last-referenced ids in the `(context)` line before every user turn pushes that reasoning into a one-line structured hint the model can actually follow.

### `pending_confirmation` — The Two-Step State Machine

```python
pending_confirmation: dict | None = None
# Shape when set:
#   {"tool": "add_note", "args": { ...merged args the preview was built from... }}
```

Set the moment any tool returns `needs_confirmation: true`. Cleared the moment any non-gated tool runs (so switching intent mid-confirmation releases the lock).

```python
# backend/agent/intent_parser.py — inside _run_tool_call
if result.needs_confirmation:
    state.pending_confirmation = {"tool": call.name, "args": effective_args}
else:
    state.pending_confirmation = None
```

The stored `args` are the **merged** arguments (after `merge_with_pending`), not the raw model output. When the user's next message says *"use tag development"*, the new tool call is merged onto these already-complete args — so the model only has to emit the diff.

See [03 - Note Agent](./03-note-agent.md) for the full merge-with-pending logic.

## The `(context)` Line — Hidden System Turn

Before every LLM call, the orchestrator injects a short hidden system message with the important structured state. This is the single biggest reason the small-model experience works at all.

```python
def _build_messages(state: SessionState) -> list[dict]:
    """System prompt, then context line, then the rolling message deque."""
    out: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    ctx = build_context_line(state)
    if ctx:
        out.append({"role": "system", "content": ctx})
    out.extend(state.messages)
    return out
```

The context line carries three pieces of information, each conditional:

### 1. Today's Date

Always included. The model otherwise defaults to its training cutoff, which breaks any relative-date reasoning ("today", "this week", "last Tuesday").

```python
now = datetime.now().astimezone()
parts.append(
    f'Today is {now.strftime("%A, %B %d, %Y")} (local time).'
)
```

The prompt tells the LLM explicitly that **this is the authoritative date** — not its training knowledge, not a guess — and that it should use it when filtering tag-listed notes by a weekday or "today".

### 2. Last Referenced Note Ids

Conditional — only included when the list is non-empty.

```python
if state.last_referenced_note_ids:
    ids = state.last_referenced_note_ids
    primary = ids[0]
    parts.append(
        f"The most recently referenced note ids are: {ids}. "
        f'"that note" / "the last one" / "it" refers to {primary}.'
    )
```

The `primary` is the first id in the list — i.e. the first result of the most recent tool call. The prompt tells the model:

- *"that note"* / *"the last one"* / *"it"* → use `primary` directly, do not search again.
- If the user mentions a specific id or description, resolve to a specific id from the list.

### 3. Pending Confirmation

Conditional — only included when a confirmation is pending.

```python
if state.pending_confirmation:
    pc = state.pending_confirmation
    tool = pc.get("tool", "<unknown>")
    args = pc.get("args") or {}
    parts.append(
        f"A `{tool}` call is awaiting confirmation with arguments {args}. "
        "Interpret the user's latest message in that context:\n"
        f"  • Affirmative ('yes', 'save it', 'confirm', 'go ahead') → call "
        f"`{tool}` again with the SAME arguments plus confirm=true.\n"
        f"  • Negative ('no', 'cancel', 'never mind') → acknowledge in plain "
        f"text and do NOT call the tool.\n"
        "  • Modification ('use tag X', 'change title to Y', 'different "
        "description') → MERGE the change into the pending arguments and "
        f"call `{tool}` again with confirm=false to re-preview. Do NOT "
        "start a new add/update from scratch — continue the pending one."
    )
```

This is essentially a micro-policy the model re-reads on every hop of a confirmation flow. It covers the three meaningful user responses (yes / no / modification) and explicitly forbids the most common mistake — starting a new add from scratch when the user says "use tag X".

### Full Context Line — Worked Example

Mid-confirmation on an add, after searching for "meeting":

```
(context) Today is Wednesday, April 22, 2026 (local time). The most recently
referenced note ids are: [3, 5]. "that note" / "the last one" / "it" refers
to 3. A `add_note` call is awaiting confirmation with arguments
{'title': 'Meeting on Tuesday 7 pm', 'description': 'Meeting with the dev
team on Tuesday at 7 pm', 'tag': None, 'confirm': False}. Interpret the
user's latest message in that context: ...
```

## The Pending-Confirmation State Machine

One picture of how `pending_confirmation` moves between `None` and `{...}` across a typical add flow:

```
    ┌──────────────┐
    │  NO PENDING  │ ◄──────────────────────────────────────┐
    └──────┬───────┘                                         │
           │                                                 │
 user asks to add note                                       │
           ▼                                                 │
    add_note(confirm=false)                                  │
    handler returns ToolResult(needs_confirmation=true)      │
           │                                                 │
           ▼                                                 │
    ┌──────────────────────────────┐                         │
    │  PENDING { tool, merged args} │                        │
    └─────┬─────────────────┬──────┘                         │
          │                 │                                │
   "use tag X" (modify)   "yes" (commit)                     │
          │                 │                                │
          ▼                 ▼                                │
   merge-with-pending  commit-intent gate                    │
   add_note(confirm=false) add_note(confirm=true)            │
          │                 │                                │
          ▼                 ▼                                │
   new preview         handler commits → pending cleared ────┘
   pending updated
   (loop)

 At any point: user runs a non-gated tool (e.g. list_notes) → pending cleared
```

The orchestrator is responsible for the two transitions (set, clear). The tool handler is responsible for signalling `needs_confirmation: true` vs a normal commit. Neither lies.

## A Full Turn, Annotated With State Changes

```
STATE before:
  messages = [... 10 earlier messages ...]
  last_referenced_note_ids = []
  pending_confirmation = None
──────────────────────────────────────────────────────────────────
USER: "add note: call mom about the vacation"
──────────────────────────────────────────────────────────────────
STATE after user-message append:
  messages.append({"role": "user", "content": "add note: call mom..."})
  (deque maxlen auto-drops oldest if full)
──────────────────────────────────────────────────────────────────
LLM emits: add_note(title=..., description=..., tag=None, confirm=false)
──────────────────────────────────────────────────────────────────
Tool handler returns:
  ToolResult(ok=false, needs_confirmation=true, data={"preview": {...}})

remember_referenced(state, result):
  _harvest_ids sees {"preview": {...}} but preview.id is not present yet,
  so last_referenced_note_ids stays [].
  (ids only start flowing once a note has been created or fetched.)

pending_confirmation = {"tool": "add_note", "args": {...merged...}}

messages appended:
  {role:"assistant", tool_calls:[{function:{name:"add_note", arguments:{...}}}]}
  {role:"tool", name:"add_note", content:"<ToolResult JSON>"}
──────────────────────────────────────────────────────────────────
LLM emits kind="message": "I'll save this note: ..."
──────────────────────────────────────────────────────────────────
messages appended:
  {role:"assistant", content:"I'll save this note: ..."}
──────────────────────────────────────────────────────────────────
Turn ends. emit("done", {}).
──────────────────────────────────────────────────────────────────
STATE after turn:
  messages = [... original ..., user, tool_call, tool_result, assistant]
  last_referenced_note_ids = []
  pending_confirmation = {"tool":"add_note", "args": {...}}
```

The next turn will start with `build_context_line` seeing the pending confirmation and generating the mid-confirmation policy text above.

## Resetting State

Two explicit reset paths:

1. **Per-session**: `store.reset(session_id)` drops one session. Not exposed via HTTP in this build, but trivial to add if needed.
2. **Per-client**: the frontend generates a new `sessionId` on "Reset". The backend's old session lingers in the dict until the process restarts; that's an acceptable leak for the assessment given the small footprint.

There is no TTL. Sessions live until the process dies. A production deployment would move `SessionStore` behind Redis with an expiration.

## Frontend State

The UI mirrors enough state to render cleanly and re-submit on retry. It deliberately does **not** mirror the model's `messages` deque — the backend owns conversation history; the frontend owns display state.

### `AppState` and the Reducer

```typescript
// app/src/app/types.ts
export interface AppState {
  sessionId: string;
  messages: ChatMessage[];
  toolCalls: ToolCallRecord[];
  isStreaming: boolean;
  model: ModelId;
  error?: string;
}
```

```typescript
// app/src/app/lib/reducer.ts
export type Action =
  | { type: "INIT_SESSION"; sessionId: string }
  | { type: "SET_MODEL"; model: ModelId }
  | { type: "USER_MESSAGE"; content: string; turnId: string }
  | { type: "ASSISTANT_DELTA"; content: string; turnId: string }
  | { type: "ASSISTANT_MESSAGE"; content: string; turnId: string }
  | { type: "TOOL_CALL_START"; call: ToolCallRecord }
  | { type: "TOOL_CALL_RESULT"; id: string; status: ToolStatus; message?: string; errorCode?: string }
  | { type: "STREAM_START" }
  | { type: "STREAM_END" }
  | { type: "STREAM_DROP" }
  | { type: "ERROR"; message: string }
  | { type: "DISMISS_ERROR" }
  | { type: "RESET" };
```

Plain React `useReducer`. No Redux, no Zustand — the state is small and local, and a reducer makes the SSE event stream straightforward to handle (each event maps to exactly one action).

### The `turnId` Trick

Every user message is tagged with a freshly-generated `turnId` (a client UUID). The assistant's streamed deltas carry the same `turnId`, so the `ASSISTANT_DELTA` reducer branch can find the current assistant bubble for this turn and append to it:

```typescript
case "ASSISTANT_DELTA": {
  const msgs = state.messages;
  const last = msgs[msgs.length - 1];
  if (last && last.role === "assistant" && last.turnId === action.turnId) {
    return {
      ...state,
      messages: [...msgs.slice(0, -1), { ...last, content: last.content + action.content }],
    };
  }
  // First delta of the turn — create a placeholder assistant message
  return { ...state, messages: [...msgs, { ...newBubble }] };
}
```

Without `turnId`, a race between a tool result and a delta could attach content to the wrong message.

### Hydration Safety

Two fields are intentionally **not** filled during server-side render:

1. `sessionId` — generated via `crypto.randomUUID()` only on the client, inside a `useEffect`. Generating during reducer `lazy-init` would produce different values on server and client, causing a hydration mismatch.
2. `model` (initial load) — defaults to `DEFAULT_MODEL` at render, then overwritten in `useEffect` by the value from `localStorage`.

```typescript
// app/src/app/page.tsx
useEffect(() => {
  if (!state.sessionId) {
    dispatch({ type: "INIT_SESSION", sessionId: newSessionId() });
  }
}, [state.sessionId]);

useEffect(() => {
  const saved = loadModel();
  if (saved !== state.model) {
    dispatch({ type: "SET_MODEL", model: saved });
  }
}, []);
```

### Model Persistence

The model selection is persisted across reloads via `localStorage`:

```typescript
// app/src/app/lib/modelStorage.ts
const STORAGE_KEY = "note-agent:model";

export function loadModel(): ModelId {
  if (typeof window === "undefined") return DEFAULT_MODEL;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return isValid(raw) ? raw : DEFAULT_MODEL;
  } catch {
    return DEFAULT_MODEL;
  }
}

export function saveModel(id: ModelId): void {
  if (typeof window === "undefined") return;
  try { window.localStorage.setItem(STORAGE_KEY, id); } catch {}
}
```

The `isValid` check defends against a stale / tampered value that is no longer in `MODEL_OPTIONS`.

### Ephemerality

`sessionId`, `messages`, `toolCalls` are **not** persisted. Reload = new session, empty history. This matches the backend (which also loses its in-memory state on restart) and keeps the mental model simple: one tab = one conversation.

## Stream-Drop Handling

When an SSE connection dies mid-turn, tool cards that were `status: "running"` would otherwise be stuck forever on a spinner. The reducer has a dedicated action:

```typescript
case "STREAM_DROP":
  return {
    ...state,
    toolCalls: state.toolCalls.map((tc) =>
      tc.status === "running"
        ? { ...tc, status: "fail", message: "Connection lost",
            endedAt: Date.now(), durationMs: Date.now() - tc.startedAt }
        : tc,
    ),
  };
```

The client-side stream reader in `app/src/app/lib/api.ts` dispatches `STREAM_DROP` in two cases:

1. The `fetch` throws or the reader throws mid-read.
2. The server closes cleanly but never sent a `done` event.

Both point to the same recovery: fail any in-flight tool cards, surface an error banner, let the user retry via the last-message ref in `ChatPanel`.

## What Is **Not** Remembered

A short, deliberate list:

- **Chat text is not persisted.** No DB table, no file. Conversations are ephemeral per process.
- **Pending confirmation is not persisted.** A backend restart loses it; the UI has no knowledge of it. If the user comes back and says "yes" after a crash, the LLM will ask what they mean.
- **Tool-call results are not stored in a long-term log.** They live only in `state.messages` for the session's lifetime and in the frontend's `toolCalls` array for the tab's lifetime.
- **No user identity.** There is no auth, no `user_id`, no RBAC. The assessment is single-user.

Each of these would be reasonable first-iteration features if the project grew — but adding them prematurely would bloat the surface and complicate the failure analysis that makes the current behaviour auditable.

## Summary

- **Backend memory** is a per-session rolling window of messages plus two derived fields — a pronoun-resolution list and a pending-confirmation slot.
- **The context line** is the single most important mechanism for small-model reliability: today's date, recent ids, pending-flow policy — all packed into one hidden system turn.
- **Pending-confirmation** is a tight state machine with two transitions (set on `needs_confirmation: true`, clear on any non-gated tool). The orchestrator never lies about the state; the tool handler never commits without an explicit `confirm=true`.
- **Frontend memory** is a reducer keyed by `turnId` so stream events cleanly merge into the right bubble. Nothing is persisted across reloads except the model pick.

## Related Docs

- [01 - Architecture](./01-architecture.md) — where `SessionStore` sits in the backend.
- [02 - Data Models](./02-data-models.md) — the dataclass for `SessionState` and the shape of the messages deque.
- [03 - Note Agent](./03-note-agent.md) — how the orchestrator uses `pending_confirmation` and the context line during tool flows.
