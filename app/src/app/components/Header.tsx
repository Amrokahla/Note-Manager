"use client";

import { RotateCcw } from "lucide-react";
import { shortSessionId } from "../lib/session";
import type { ModelId } from "../types";
import ModelSelector from "./ModelSelector";
import UserBadge from "./UserBadge";

interface Props {
  sessionId: string;
  model: ModelId;
  isStreaming: boolean;
  onReset: () => void;
  onModelChange: (next: ModelId) => void;
}

export default function Header({
  sessionId,
  model,
  isStreaming,
  onReset,
  onModelChange,
}: Props) {
  return (
    <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-3 shadow-sm">
      <h1 className="text-base font-semibold text-[color:var(--color-petrol)]">
        Note Agent
      </h1>
      <div className="flex items-center gap-3 text-xs text-slate-500">
        <ModelSelector
          value={model}
          disabled={isStreaming}
          onChange={onModelChange}
        />
        <UserBadge />
        {sessionId && (
          <span className="font-mono">
            session · {shortSessionId(sessionId)}
          </span>
        )}
        <button
          type="button"
          onClick={onReset}
          className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-slate-600 transition-colors hover:border-[color:var(--color-petrol)]/40 hover:text-[color:var(--color-petrol)]"
        >
          <RotateCcw size={12} />
          Reset
        </button>
      </div>
    </header>
  );
}
