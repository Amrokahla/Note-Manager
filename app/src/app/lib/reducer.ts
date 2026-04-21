import type { AppState, ModelId, ToolCallRecord, ToolStatus } from "../types";
import { DEFAULT_MODEL } from "../types";
import { newSessionId } from "./session";

export type Action =
  | { type: "INIT_SESSION"; sessionId: string }
  | { type: "SET_MODEL"; model: ModelId }
  | { type: "USER_MESSAGE"; content: string; turnId: string }
  | { type: "ASSISTANT_DELTA"; content: string; turnId: string }
  | { type: "ASSISTANT_MESSAGE"; content: string; turnId: string }
  | { type: "TOOL_CALL_START"; call: ToolCallRecord }
  | {
      type: "TOOL_CALL_RESULT";
      id: string;
      status: ToolStatus;
      message?: string;
      errorCode?: string;
    }
  | { type: "STREAM_START" }
  | { type: "STREAM_END" }
  | { type: "STREAM_DROP" }
  | { type: "ERROR"; message: string }
  | { type: "DISMISS_ERROR" }
  | { type: "RESET" };

// The initial state is intentionally deterministic — sessionId is empty.
// Generating a UUID here would run during SSR and again during client
// hydration with a different value, causing a hydration mismatch. The page
// dispatches INIT_SESSION from a useEffect after mount to fill it in.
export function initialState(seed?: Partial<AppState>): AppState {
  return {
    sessionId: seed?.sessionId ?? "",
    messages: seed?.messages ?? [],
    toolCalls: seed?.toolCalls ?? [],
    isStreaming: false,
    model: seed?.model ?? DEFAULT_MODEL,
    error: undefined,
  };
}

export function appReducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "INIT_SESSION":
      // Idempotent: ignore a second init if we already have a session.
      return state.sessionId ? state : { ...state, sessionId: action.sessionId };

    case "SET_MODEL":
      return state.model === action.model ? state : { ...state, model: action.model };

    case "USER_MESSAGE":
      return {
        ...state,
        messages: [
          ...state.messages,
          {
            id: crypto.randomUUID(),
            role: "user",
            content: action.content,
            createdAt: Date.now(),
            turnId: action.turnId,
          },
        ],
      };

    case "ASSISTANT_DELTA": {
      // Append to the last assistant message for this turn. If none exists
      // yet (first delta of the turn), create a placeholder we'll keep
      // appending to. The final ASSISTANT_MESSAGE will overwrite the content
      // with the server's authoritative version — cheap safety net in case
      // the stream and the final differ.
      const msgs = state.messages;
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant" && last.turnId === action.turnId) {
        return {
          ...state,
          messages: [
            ...msgs.slice(0, -1),
            { ...last, content: last.content + action.content },
          ],
        };
      }
      return {
        ...state,
        messages: [
          ...msgs,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: action.content,
            createdAt: Date.now(),
            turnId: action.turnId,
          },
        ],
      };
    }

    case "ASSISTANT_MESSAGE": {
      // If we already streamed an assistant message for this turn, finalize
      // it (replace content in case of drift, keep id and createdAt).
      // Otherwise create a fresh one (non-streaming path).
      const msgs = state.messages;
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant" && last.turnId === action.turnId) {
        return {
          ...state,
          messages: [
            ...msgs.slice(0, -1),
            { ...last, content: action.content },
          ],
        };
      }
      return {
        ...state,
        messages: [
          ...msgs,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: action.content,
            createdAt: Date.now(),
            turnId: action.turnId,
          },
        ],
      };
    }

    case "TOOL_CALL_START":
      return { ...state, toolCalls: [...state.toolCalls, action.call] };

    case "TOOL_CALL_RESULT":
      return {
        ...state,
        toolCalls: state.toolCalls.map((tc) =>
          tc.id === action.id
            ? {
                ...tc,
                status: action.status,
                message: action.message,
                errorCode: action.errorCode,
                endedAt: Date.now(),
                durationMs: Date.now() - tc.startedAt,
              }
            : tc,
        ),
      };

    case "STREAM_START":
      return { ...state, isStreaming: true, error: undefined };

    case "STREAM_END":
      return { ...state, isStreaming: false };

    case "STREAM_DROP":
      // Connection died mid-turn. Flip every card that was still "running"
      // to "fail" so the UI doesn't leave a stale spinner behind.
      return {
        ...state,
        toolCalls: state.toolCalls.map((tc) =>
          tc.status === "running"
            ? {
                ...tc,
                status: "fail",
                message: "Connection lost",
                endedAt: Date.now(),
                durationMs: Date.now() - tc.startedAt,
              }
            : tc,
        ),
      };

    case "ERROR":
      return { ...state, isStreaming: false, error: action.message };

    case "DISMISS_ERROR":
      return { ...state, error: undefined };

    case "RESET":
      // Fired from a click handler — always client-side, so generating a
      // fresh UUID here is safe (no SSR path). Preserve the current model
      // choice so a reset doesn't wipe the user's selector pick.
      return initialState({ sessionId: newSessionId(), model: state.model });
  }
}
