import type { ModelId, ToolCallRecord, ToolStatus } from "../types";

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
  continues_pending?: boolean;
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
  model: ModelId,
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
      body: JSON.stringify({ session_id: sessionId, message, model }),
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

      let boundary: number;
      while ((boundary = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, boundary);
        buf = buf.slice(boundary + 2);
        const dispatched = dispatchFrame(frame, turnId, handlers);
        if (dispatched === "done") sawDone = true;
      }
    }
  } catch {
    handlers.onStreamDrop();
    handlers.onError("Connection to the agent was lost.");
    handlers.onDone();
    return;
  }

  if (!sawDone) {
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
    if (line.startsWith(":")) continue;
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
        continuesPending: p.continues_pending ?? false,
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
