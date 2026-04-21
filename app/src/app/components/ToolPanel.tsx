"use client";

import type { ToolCallRecord } from "../types";
import ToolCallCard from "./ToolCallCard";

export default function ToolPanel({ toolCalls }: { toolCalls: ToolCallRecord[] }) {
  return (
    <aside
      role="log"
      aria-live="polite"
      className="flex min-h-0 flex-col border-l border-neutral-200 bg-neutral-50/60"
    >
      <h2 className="border-b border-neutral-200 px-4 py-3 text-sm font-semibold text-neutral-700">
        Tool calls
      </h2>
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {toolCalls.length === 0 ? (
          <p className="mt-6 text-center text-xs text-neutral-500">
            Tool calls will appear here as the agent works.
          </p>
        ) : (
          toolCalls.map((tc) => <ToolCallCard key={tc.id} call={tc} />)
        )}
      </div>
    </aside>
  );
}
