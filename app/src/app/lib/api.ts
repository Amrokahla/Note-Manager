import type { ToolCallRecord, ToolStatus } from "../types";

// F2: non-streaming POST /chat per FRONTEND_PLAN §4.1. The handler surface
// below intentionally matches the shape F3's SSE stream will emit, so the
// ChatPanel wiring doesn't change when we upgrade transports.

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

interface BackendToolResult {
  ok: boolean;
  message: string;
  data?: unknown;
  needs_confirmation?: boolean;
  candidates?: unknown[] | null;
  error_code?: string | null;
}

interface BackendToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  result: BackendToolResult;
}

interface ChatResponse {
  session_id: string;
  reply: string;
  tool_calls: BackendToolCall[];
}

function statusFromResult(result: BackendToolResult): ToolStatus {
  if (result.needs_confirmation) return "needs_confirmation";
  if (result.ok) return "ok";
  return "fail";
}

export async function sendMessage(
  sessionId: string,
  message: string,
  turnId: string,
  handlers: ChatHandlers,
): Promise<void> {
  const baseUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

  let res: Response;
  try {
    res = await fetch(`${baseUrl}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message }),
    });
  } catch {
    handlers.onError(
      "Couldn't reach the agent. Is the backend running on " + baseUrl + "?",
    );
    handlers.onDone();
    return;
  }

  if (!res.ok) {
    handlers.onError(`Backend returned HTTP ${res.status}.`);
    handlers.onDone();
    return;
  }

  let data: ChatResponse;
  try {
    data = (await res.json()) as ChatResponse;
  } catch {
    handlers.onError("Backend sent a response that wasn't valid JSON.");
    handlers.onDone();
    return;
  }

  // Emit tool_call + tool_result as distinct events even though they arrive
  // simultaneously in F2. This keeps the reducer API identical to what the
  // SSE path in F3 will produce — components never need to change.
  const now = Date.now();
  for (const tc of data.tool_calls ?? []) {
    handlers.onToolCall({
      id: tc.id,
      turnId,
      name: tc.name,
      arguments: tc.arguments,
      status: "running",
      startedAt: now,
    });
    handlers.onToolResult({
      id: tc.id,
      status: statusFromResult(tc.result),
      message: tc.result.message,
      errorCode: tc.result.error_code ?? undefined,
    });
  }

  handlers.onAssistant(data.reply);
  handlers.onDone();
}
