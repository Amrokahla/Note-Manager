"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import type { ToolCallRecord } from "../types";
import StatusBadge from "./StatusBadge";

export default function ToolCallCard({ call }: { call: ToolCallRecord }) {
  const [expanded, setExpanded] = useState(false);

  const messageColor =
    call.status === "fail"
      ? "text-rose-700"
      : call.status === "needs_confirmation"
        ? "text-amber-700"
        : "text-slate-600";

  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-white text-xs shadow-sm">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left transition-colors hover:bg-slate-50"
      >
        <span className="flex items-center gap-1.5">
          {expanded ? (
            <ChevronDown size={12} className="text-slate-400" />
          ) : (
            <ChevronRight size={12} className="text-slate-400" />
          )}
          <code className="font-mono text-[13px] font-semibold text-[color:var(--color-petrol)]">
            {call.name}
          </code>
        </span>
        <StatusBadge status={call.status} />
      </button>

      {expanded && (
        <div className="border-t border-slate-100 px-3 pb-3 pt-2">
          <pre className="max-h-48 overflow-auto rounded bg-slate-50 p-2 text-[11px] whitespace-pre-wrap break-words text-slate-600">
            {JSON.stringify(call.arguments, null, 2)}
          </pre>

          {call.message && (
            <p className={`mt-2 ${messageColor}`}>{call.message}</p>
          )}

          {call.durationMs !== undefined && (
            <p className="mt-1 text-[10px] text-slate-400">{call.durationMs} ms</p>
          )}
        </div>
      )}
    </div>
  );
}
