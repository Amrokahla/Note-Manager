import type { ToolCallRecord, ToolStatus } from "../types";

// F1: stubbed — real wiring lands in F2 when the backend POST /chat returns
// { reply, tool_calls } (FRONTEND_PLAN §4.1). The handler surface below is
// what the SSE version in F3 will also satisfy, so components can be built
// against this contract today.

export interface ChatHandlers {
  onUserEcho: (m: string) => void;
  onToolCall: (c: ToolCallRecord) => void;
  onToolResult: (r: {
    id: string;
    status: ToolStatus;
    message?: string;
    errorCode?: string;
  }) => void;
  onAssistant: (content: string) => void;
  onDone: () => void;
  onError: (err: string) => void;
}

export async function sendMessage(
  _sessionId: string,
  _message: string,
  handlers: ChatHandlers,
): Promise<void> {
  handlers.onError("Backend /chat not wired yet — this lands in frontend F2.");
}
