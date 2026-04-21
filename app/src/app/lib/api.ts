import type { ToolCallRecord, ToolStatus } from "../types";

// F3: streaming POST /chat/stream per FRONTEND_PLAN §4.2. The backend is an
// SSE source whose event names are: user_echo, tool_call, tool_result,
// assistant, done, error. We parse the stream frame-by-frame and dispatch
// events through the same handler interface F2 introduced — components don't
// change when transports swap.

export interface ChatHandlers {
  onUserEcho: (m: string) => void;
  onToolCall: (c: ToolCallRecord) => void;
  onToolResult: (r: {
    id: string;
    status: ToolStatus;
    message?: string;
    errorCode?: string;
  }) => void;
  onAssistantDelta: (delta: string) => void;
  onAssistant: (content: string) => void;
  onDone: () => void;
  onError: (err: string) => void;
  onStreamDrop: () => void;
}

type ServerToolStatus = "running" | "ok" | "fail" | "needs_confirmation";

interface UserEchoPayload {
  message: string;
}
interface ToolCallPayload {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  status: ServerToolStatus;
}
interface ToolResultPayload {
  id: string;
  status: ServerToolStatus;
  message?: string;
  error_code?: string | null;
  data?: unknown;
  needs_confirmation?: boolean;
  candidates?: unknown[] | null;
}
interface AssistantPayload {
  content: string;
}
interface ErrorPayload {
  message: string;
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
    res = await fetch(`${baseUrl}/chat/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({ session_id: sessionId, message }),
    });
  } catch {
    handlers.onError(
      `Couldn't reach the agent at ${baseUrl}. Is the backend running?`,
    );
    handlers.onDone();
    return;
  }

  if (!res.ok || !res.body) {
    handlers.onError(`Backend returned HTTP ${res.status}.`);
    handlers.onDone();
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let sawDone = false;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buf += decoder.decode(value, { stream: true });

      // SSE frame separator is a blank line — i.e. \n\n.
      let boundary: number;
      while ((boundary = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, boundary);
        buf = buf.slice(boundary + 2);
        const dispatched = dispatchFrame(frame, turnId, handlers);
        if (dispatched === "done") sawDone = true;
      }
    }
  } catch {
    // Stream broke mid-turn — tell the caller so it can fail any in-flight
    // tool cards, then surface a human-friendly error.
    handlers.onStreamDrop();
    handlers.onError("Connection to the agent was lost.");
    handlers.onDone();
    return;
  }

  if (!sawDone) {
    // Server closed cleanly but never sent a done frame — same handling as a
    // hard drop: fail any in-flight tool calls.
    handlers.onStreamDrop();
  }
  handlers.onDone();
}

function dispatchFrame(
  frame: string,
  turnId: string,
  handlers: ChatHandlers,
): "done" | "continue" {
  let eventType = "message";
  let dataLine = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue; // SSE comment / keep-alive
    if (line.startsWith("event: ")) eventType = line.slice("event: ".length).trim();
    else if (line.startsWith("data: ")) dataLine = line.slice("data: ".length);
  }
  if (!dataLine) return "continue";

  let payload: unknown;
  try {
    payload = JSON.parse(dataLine);
  } catch {
    return "continue";
  }

  switch (eventType) {
    case "user_echo":
      handlers.onUserEcho((payload as UserEchoPayload).message);
      return "continue";

    case "tool_call": {
      const p = payload as ToolCallPayload;
      handlers.onToolCall({
        id: p.id,
        turnId,
        name: p.name,
        arguments: p.arguments,
        status: "running",
        startedAt: Date.now(),
      });
      return "continue";
    }

    case "tool_result": {
      const p = payload as ToolResultPayload;
      handlers.onToolResult({
        id: p.id,
        status: p.status,
        message: p.message,
        errorCode: p.error_code ?? undefined,
      });
      return "continue";
    }

    case "assistant_delta":
      handlers.onAssistantDelta((payload as AssistantPayload).content);
      return "continue";

    case "assistant":
      handlers.onAssistant((payload as AssistantPayload).content);
      return "continue";

    case "error":
      handlers.onError((payload as ErrorPayload).message);
      return "continue";

    case "done":
      return "done";

    default:
      return "continue";
  }
}
