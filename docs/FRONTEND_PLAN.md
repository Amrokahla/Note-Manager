# Frontend Plan — Note Agent UI (Next.js)

> Lives in `/app`. Single-page application. Two-column layout: **70% chat / 30% tool-call context**.
> Keeps things minimal on purpose — the assessment says UI is not evaluated, but a clean debug-friendly UI helps the grader *see* the agent thinking.

---

## 1. Goals & Non-Goals

### Goals
1. **Conversational UX**: send messages to the backend, stream replies, show history.
2. **Transparency**: render every tool the agent called this turn, with status (ok / fail / needs-confirmation) and a compact args preview.
3. **Debuggability**: the grader can watch the loop work in real time.
4. **Zero polish tax**: no design system, no auth screens, no routing complexity.

### Non-Goals
- No multi-page app, no settings page, no note editor UI.
- No interaction from the tool panel (read-only this milestone).
- No dark/light toggle, no animations beyond basic fades.
- No auth UI.

---

## 2. Tech Stack

| Concern | Choice | Why |
|---|---|---|
| Framework | **Next.js 15 (App Router)** | Modern default; single-page is trivial with one `page.tsx` |
| Language | **TypeScript** | Type-safety for the chat/tool contracts |
| Styling | **Tailwind CSS v4** | Fastest path to a clean two-column layout |
| State | **React `useState` / `useReducer`** | Single page, no need for Redux/Zustand |
| HTTP | **`fetch`** with SSE (`ReadableStream`) | Streams assistant replies + tool events |
| Icons | **lucide-react** | Light, tree-shakable |
| Runtime | Node 20 | Matches Next.js 15 baseline |

No tRPC, no React Query, no form library. Adding them would cost more than they save for one screen.

---

## 3. Layout (Single Screen)

```
┌────────────────────────────────────────────────────────────────────────┐
│  Header: "Note Agent"   [session id · reset]                           │
├──────────────────────────────────────────┬─────────────────────────────┤
│                                          │                             │
│                                          │   Tool Calls (read-only)    │
│            Chat (70%)                    │         (30%)               │
│                                          │                             │
│   ┌───────────────────────────────┐      │   ┌───────────────────────┐ │
│   │ User:   "Add a note about…"   │      │   │ ✔ add_note            │ │
│   └───────────────────────────────┘      │   │   title:"standup"     │ │
│   ┌───────────────────────────────┐      │   └───────────────────────┘ │
│   │ Agent:  "Saved! Note #17."    │      │   ┌───────────────────────┐ │
│   └───────────────────────────────┘      │   │ ✗ search_notes        │ │
│                                          │   │   No notes matched    │ │
│                                          │   └───────────────────────┘ │
│                                          │                             │
│   ┌───────────────────────────────────┐  │                             │
│   │  [Type a message…]        [Send]  │  │                             │
│   └───────────────────────────────────┘  │                             │
└──────────────────────────────────────────┴─────────────────────────────┘
```

- Two-column CSS grid: `grid-cols-[7fr_3fr]` on ≥ `lg`, stacked on smaller screens (chat on top, tools collapsed).
- Header: app name + session id (short form) + **Reset** button (new `sessionId`, clears state).
- Chat column: scrollable message list (auto-scroll to bottom on new message) + sticky input at the bottom.
- Tool column: scrollable timeline, newest at the bottom, grouped by **turn** so the grader can visually pair a chat turn with its tool calls.

---

## 4. Data Contract with Backend

Two endpoints are expected from the backend (already described in `PLAN.md`):

### 4.1 `POST /chat` (non-streaming fallback)
Request:
```json
{ "session_id": "uuid-v4", "message": "Add a note about standup" }
```
Response:
```json
{
  "reply": "Saved! Note #17.",
  "tool_calls": [
    {
      "id": "tc_1",
      "name": "add_note",
      "arguments": { "title": "standup", "body": "…", "tags": ["meetings"] },
      "result": { "ok": true, "message": "Created note #17", "data": { "id": 17 } }
    }
  ]
}
```

### 4.2 `GET /chat/stream` (SSE — preferred)
Server-Sent Events, one event per stage, so the UI can show tool calls *as they happen*:

```
event: user_echo
data: {"message":"Add a note about standup"}

event: tool_call
data: {"id":"tc_1","name":"add_note","arguments":{...},"status":"running"}

event: tool_result
data: {"id":"tc_1","status":"ok","message":"Created note #17","data":{"id":17}}

event: assistant
data: {"content":"Saved! Note #17."}

event: done
data: {}
```

**Frontend handling rule:** every `tool_call` event creates a new card in "pending" state in the right panel, and the matching `tool_result` flips it to ok/fail. This gives the grader a live view of the loop.

If SSE is not implemented in backend v1, fall back to the non-streaming response — the UI logic is the same, just flushed at once.

---

## 5. Type Definitions (frontend-side)

```ts
// app/types.ts
export type Role = "user" | "assistant";

export interface ChatMessage {
  id: string;          // client-side uuid
  role: Role;
  content: string;
  createdAt: number;   // Date.now()
  turnId: string;      // groups a user msg + its assistant reply + tool calls
}

export type ToolStatus = "running" | "ok" | "fail" | "needs_confirmation";

export interface ToolCallRecord {
  id: string;                 // backend tool-call id
  turnId: string;             // which user turn triggered it
  name: string;               // e.g. "add_note"
  arguments: Record<string, unknown>;
  status: ToolStatus;
  message?: string;           // from ToolResult.message
  errorCode?: string;         // from ToolResult.error_code
  durationMs?: number;
  startedAt: number;
  endedAt?: number;
}

export interface AppState {
  sessionId: string;
  messages: ChatMessage[];
  toolCalls: ToolCallRecord[];
  isStreaming: boolean;
  error?: string;
}
```

The `turnId` is the primary joiner between columns — a tool card knows which chat turn it belongs to, so we can optionally highlight the pair on hover later.

---

## 6. File Structure Inside `/app`

```
app/
├── package.json
├── tsconfig.json
├── next.config.ts
├── tailwind.config.ts
├── postcss.config.mjs
├── .env.local.example           # NEXT_PUBLIC_API_URL=http://localhost:8000
├── public/
│   └── favicon.ico
└── src/
    └── app/
        ├── layout.tsx           # HTML shell, font, global CSS
        ├── globals.css          # Tailwind directives
        ├── page.tsx             # THE single page (the 70/30 layout)
        ├── types.ts             # shared types (above)
        ├── lib/
        │   ├── api.ts           # fetch/SSE client for /chat
        │   └── session.ts       # create/reset session id
        └── components/
            ├── Header.tsx
            ├── ChatPanel.tsx        # left 70%
            ├── MessageList.tsx
            ├── MessageBubble.tsx
            ├── Composer.tsx         # input + send button
            ├── ToolPanel.tsx        # right 30%
            ├── ToolCallCard.tsx
            └── StatusBadge.tsx
```

Kept flat on purpose; each component is < 80 lines.

---

## 7. Component Responsibilities & Snippets

### 7.1 `page.tsx` — the only route

```tsx
"use client";
import { useReducer } from "react";
import Header from "./components/Header";
import ChatPanel from "./components/ChatPanel";
import ToolPanel from "./components/ToolPanel";
import { appReducer, initialState } from "./lib/reducer";

export default function Home() {
  const [state, dispatch] = useReducer(appReducer, undefined, initialState);

  return (
    <main className="h-dvh flex flex-col">
      <Header sessionId={state.sessionId}
              onReset={() => dispatch({ type: "RESET" })} />
      <div className="grid grid-cols-1 lg:grid-cols-[7fr_3fr] flex-1 min-h-0">
        <ChatPanel state={state} dispatch={dispatch} />
        <ToolPanel toolCalls={state.toolCalls} />
      </div>
    </main>
  );
}
```

### 7.2 State: `lib/reducer.ts`

A small reducer keeps the SSE handler simple (each event is one `dispatch`).

```ts
type Action =
  | { type: "USER_MESSAGE"; content: string; turnId: string }
  | { type: "ASSISTANT_MESSAGE"; content: string; turnId: string }
  | { type: "TOOL_CALL_START"; call: ToolCallRecord }
  | { type: "TOOL_CALL_RESULT"; id: string; status: ToolStatus;
      message?: string; errorCode?: string }
  | { type: "STREAM_START" } | { type: "STREAM_END" }
  | { type: "ERROR"; message: string }
  | { type: "RESET" };

export function appReducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "USER_MESSAGE":
      return { ...state, messages: [...state.messages, {
        id: crypto.randomUUID(), role: "user",
        content: action.content, createdAt: Date.now(),
        turnId: action.turnId,
      }]};
    case "ASSISTANT_MESSAGE":
      return { ...state, messages: [...state.messages, {
        id: crypto.randomUUID(), role: "assistant",
        content: action.content, createdAt: Date.now(),
        turnId: action.turnId,
      }]};
    case "TOOL_CALL_START":
      return { ...state, toolCalls: [...state.toolCalls, action.call] };
    case "TOOL_CALL_RESULT":
      return { ...state, toolCalls: state.toolCalls.map(tc =>
        tc.id === action.id
          ? { ...tc, status: action.status, message: action.message,
              errorCode: action.errorCode, endedAt: Date.now(),
              durationMs: Date.now() - tc.startedAt }
          : tc) };
    case "STREAM_START": return { ...state, isStreaming: true, error: undefined };
    case "STREAM_END":   return { ...state, isStreaming: false };
    case "ERROR":        return { ...state, isStreaming: false, error: action.message };
    case "RESET":        return initialState();
  }
}
```

### 7.3 `lib/api.ts` — SSE with fallback

```ts
export async function sendMessage(
  sessionId: string,
  message: string,
  handlers: {
    onUserEcho: (m: string) => void;
    onToolCall: (c: ToolCallRecord) => void;
    onToolResult: (r: { id: string; status: ToolStatus; message?: string;
                        errorCode?: string }) => void;
    onAssistant: (content: string) => void;
    onDone: () => void;
    onError: (err: string) => void;
  }
) {
  const url = `${process.env.NEXT_PUBLIC_API_URL}/chat/stream`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!res.ok || !res.body) { handlers.onError(`HTTP ${res.status}`); return; }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // Parse SSE frames (split on \n\n), dispatch per event type…
    // Implementation ~30 lines; omitted for brevity here.
  }
  handlers.onDone();
}
```

### 7.4 `ChatPanel.tsx`

- Renders `<MessageList/>` (flex-1, overflow-y-auto) and `<Composer/>` pinned to bottom.
- Owns the submit handler, which:
  1. dispatches `USER_MESSAGE` + `STREAM_START`.
  2. calls `sendMessage` with handlers that dispatch the right actions.
  3. dispatches `STREAM_END` at the end.

### 7.5 `MessageBubble.tsx`

- Two styles based on `role`:
  - User: right-aligned, subtle colored bubble.
  - Assistant: left-aligned, plain surface, monospaced tool summaries inline if needed.
- Markdown rendering: **skipped** for v1. We display plain text + `\n` → `<br>` (`white-space: pre-wrap`). Keeps dependencies minimal.

### 7.6 `ToolPanel.tsx`

```tsx
export default function ToolPanel({ toolCalls }: { toolCalls: ToolCallRecord[] }) {
  return (
    <aside className="border-l border-neutral-200 bg-neutral-50/50 flex flex-col min-h-0">
      <h2 className="px-4 py-3 text-sm font-semibold text-neutral-700 border-b">
        Tool calls
      </h2>
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {toolCalls.length === 0 && (
          <p className="text-xs text-neutral-500 text-center mt-6">
            Tool calls will appear here as the agent works.
          </p>
        )}
        {toolCalls.map(tc => <ToolCallCard key={tc.id} call={tc} />)}
      </div>
    </aside>
  );
}
```

### 7.7 `ToolCallCard.tsx`

A compact, non-interactive card:

```tsx
export default function ToolCallCard({ call }: { call: ToolCallRecord }) {
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3 text-xs">
      <div className="flex items-center justify-between">
        <code className="font-mono font-semibold text-[13px]">{call.name}</code>
        <StatusBadge status={call.status} />
      </div>

      {/* args preview: one-line JSON, truncated */}
      <pre className="mt-2 text-[11px] text-neutral-600 whitespace-pre-wrap break-words
                       bg-neutral-50 rounded p-2 max-h-28 overflow-hidden">
{JSON.stringify(call.arguments, null, 2)}
      </pre>

      {call.message && (
        <p className={`mt-2 ${call.status === "fail" ? "text-red-600" : "text-neutral-600"}`}>
          {call.message}
        </p>
      )}
      {call.durationMs !== undefined && (
        <p className="mt-1 text-[10px] text-neutral-400">{call.durationMs} ms</p>
      )}
    </div>
  );
}
```

### 7.8 `StatusBadge.tsx`

Small color-coded pill:

| status | label | color |
|---|---|---|
| `running` | "running…" | neutral + spinner |
| `ok` | "ok" | green |
| `fail` | "fail" | red |
| `needs_confirmation` | "confirm?" | amber |

Uses `lucide-react` icons (`Check`, `X`, `Loader2`, `ShieldAlert`).

---

## 8. Visual Design (intentional minimalism)

- **Font:** system-ui stack; Next's `next/font` with Inter if we want a touch more polish.
- **Colors:**
  - Background: `bg-white` / `bg-neutral-50`.
  - Primary accent: `indigo-600` for the Send button + user bubble outline.
  - Status colors: `emerald-500` (ok), `rose-500` (fail), `amber-500` (confirm), `neutral-400` (running).
- **Spacing:** generous padding in the chat column (`p-6`), tighter in the tool panel (`p-3`).
- **Scrollbars:** default.
- **Focus rings:** keep Tailwind defaults — they're already accessible.

No custom illustrations, no logo. The grader should see a tool, not a brand.

---

## 9. Accessibility Basics (free wins)

- Semantic regions: `<main>`, `<aside>`, `<header>`.
- The composer is a `<form>` with `<label class="sr-only">` for the input.
- Send button is disabled while `isStreaming`.
- `aria-live="polite"` on the message list announces new assistant messages.
- Tool panel has `role="log"` + `aria-live="polite"` so screen readers get tool activity too.

---

## 10. Error States

| Situation | UI |
|---|---|
| Backend unreachable | Red banner above composer: "Couldn't reach the agent. Is Ollama running?" with Retry |
| Stream drops mid-turn | Last in-flight tool card flips to `fail` with message "Connection lost" |
| User submits empty msg | Composer disables Send until text is non-empty |
| `needs_confirmation` tool | Card shows amber badge + message; chat bubble carries the agent's "are you sure?" question |

All error surface is passive — no modals, no toasts library.

---

## 11. Build Milestones (frontend-specific)

Aligned with the backend phases in `PLAN.md`; frontend can start at backend **Phase 3**.

| # | Milestone | Depends on backend phase | Output |
|---|---|---|---|
| F0 | Scaffold Next.js + Tailwind in `/app` | — | `next dev` renders empty 70/30 layout |
| F1 | Static components + mock data | — | `ChatPanel` + `ToolPanel` render fake turns from a fixture |
| F2 | Wire non-streaming `/chat` | Backend P3–P7 | Full round-trip works, tool cards appear after reply |
| F3 | Switch to SSE streaming | Backend adds `/chat/stream` | Tool cards animate in as the agent thinks |
| F4 | Error states + reset + aria | — | Submission-ready |
| F5 (opt) | Keyboard shortcuts (Cmd/Ctrl+Enter, Cmd/Ctrl+K reset) | — | Nice-to-have |

F0 and F1 can happen in parallel with backend P0–P2.

---

## 12. Definition of Done (for the whole UI milestone)

- `npm run dev` inside `/app` brings up `http://localhost:3000`.
- Typing a message and hitting Send results in:
  1. The user bubble appearing immediately in the chat column.
  2. One or more cards appearing in the tool column as tools are called (running → ok/fail).
  3. The assistant bubble appearing when the turn ends.
- The **Reset** button clears both columns and generates a new session id.
- Looks clean on a 1280×800 laptop screen and on a 375px mobile viewport (stacked).
- No console errors during a happy-path turn, a destructive-confirmation turn, and an ambiguous-search turn.

---

## 13. Out-of-Scope (explicitly, so we don't drift)

- Editing notes directly in the UI.
- Clicking a tool card to replay it.
- Showing note contents outside of what the assistant says.
- Theme switching, animations, drag-to-resize columns.
- Persisting chat history across refreshes (session dies with the tab — intentional for a demo).

If any of these become needed, we'd add them only after the backend evaluation harness is green.
