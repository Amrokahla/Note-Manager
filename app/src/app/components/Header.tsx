"use client";

import { RotateCcw } from "lucide-react";
import { shortSessionId } from "../lib/session";

interface Props {
  sessionId: string;
  onReset: () => void;
}

export default function Header({ sessionId, onReset }: Props) {
  return (
    <header className="flex items-center justify-between border-b border-neutral-200 bg-white px-6 py-3">
      <h1 className="text-base font-semibold text-neutral-900">Note Agent</h1>
      <div className="flex items-center gap-3 text-xs text-neutral-500">
        <span className="font-mono">session · {shortSessionId(sessionId)}</span>
        <button
          type="button"
          onClick={onReset}
          className="inline-flex items-center gap-1 rounded-md border border-neutral-200 bg-white px-2 py-1 text-neutral-600 transition-colors hover:bg-neutral-50 hover:text-neutral-900"
        >
          <RotateCcw size={12} />
          Reset
        </button>
      </div>
    </header>
  );
}
