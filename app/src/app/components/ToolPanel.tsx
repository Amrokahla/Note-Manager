"use client";

import type { ToolCallRecord } from "../types";
import ToolCallCard from "./ToolCallCard";

export default function ToolPanel({ toolCalls }: { toolCalls: ToolCallRecord[] }) {
  return (
    <aside
      role="log"
      aria-live="polite"
      className="flex min-h-0 flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm"
    >
      <h2 className="border-b border-slate-200 bg-[color:var(--color-petrol-soft)] px-4 py-3 text-sm font-semibold text-[color:var(--color-petrol)]">
        Tool calls
      </h2>
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {toolCalls.length === 0 ? (
          <p className="mt-6 text-center text-xs text-slate-500">
            Tool calls will appear here as the agent works.
          </p>
        ) : (
          toolCalls.map((tc) => <ToolCallCard key={tc.id} call={tc} />)
        )}
      </div>
    </aside>
  );
}
