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
      return initialState({ sessionId: newSessionId(), model: state.model });
  }
}
