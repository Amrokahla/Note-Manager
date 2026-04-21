export type Role = "user" | "assistant";

export interface ChatMessage {
  id: string;
  role: Role;
  content: string;
  createdAt: number;
  turnId: string;
}

export type ToolStatus = "running" | "ok" | "fail" | "needs_confirmation";

export interface ToolCallRecord {
  id: string;
  turnId: string;
  name: string;
  arguments: Record<string, unknown>;
  status: ToolStatus;
  message?: string;
  errorCode?: string;
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
