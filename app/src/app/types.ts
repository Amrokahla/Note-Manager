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

export type ModelId = "ollama" | "gemini-2.5-pro" | "gemini-2.5-flash";

export const MODEL_OPTIONS: { id: ModelId; label: string }[] = [
  { id: "gemini-2.5-pro", label: "Gemini 2.5 Pro" },
  { id: "gemini-2.5-flash", label: "Gemini 2.5 Flash" },
  { id: "ollama", label: "Ollama (llama3.1)" },
];

export const DEFAULT_MODEL: ModelId = "gemini-2.5-pro";

export interface AppState {
  sessionId: string;
  messages: ChatMessage[];
  toolCalls: ToolCallRecord[];
  isStreaming: boolean;
  model: ModelId;
  error?: string;
}
