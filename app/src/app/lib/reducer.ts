import type { AppState, ToolCallRecord, ToolStatus } from "../types";
import { newSessionId } from "./session";

export type Action =
  | { type: "INIT_SESSION"; sessionId: string }
  | { type: "USER_MESSAGE"; content: string; turnId: string }
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
  | { type: "ERROR"; message: string }
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
    error: undefined,
  };
}

export function appReducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "INIT_SESSION":
      // Idempotent: ignore a second init if we already have a session.
      return state.sessionId ? state : { ...state, sessionId: action.sessionId };

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

    case "ASSISTANT_MESSAGE":
      return {
        ...state,
        messages: [
          ...state.messages,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: action.content,
            createdAt: Date.now(),
            turnId: action.turnId,
          },
        ],
      };

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

    case "ERROR":
      return { ...state, isStreaming: false, error: action.message };

    case "RESET":
      // Fired from a click handler — always client-side, so generating a
      // fresh UUID here is safe (no SSR path).
      return initialState({ sessionId: newSessionId() });
  }
}
