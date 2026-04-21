"use client";

import type { ToolCallRecord } from "../types";
import StatusBadge from "./StatusBadge";

export default function ToolCallCard({ call }: { call: ToolCallRecord }) {
  const messageColor =
    call.status === "fail"
      ? "text-rose-700"
      : call.status === "needs_confirmation"
        ? "text-amber-700"
        : "text-neutral-600";

  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3 text-xs shadow-sm">
      <div className="flex items-center justify-between gap-2">
        <code className="font-mono text-[13px] font-semibold text-neutral-900">
          {call.name}
        </code>
        <StatusBadge status={call.status} />
      </div>

      <pre className="mt-2 max-h-32 overflow-hidden rounded bg-neutral-50 p-2 text-[11px] whitespace-pre-wrap break-words text-neutral-600">
        {JSON.stringify(call.arguments, null, 2)}
      </pre>

      {call.message && (
        <p className={`mt-2 ${messageColor}`}>{call.message}</p>
      )}

      {call.durationMs !== undefined && (
        <p className="mt-1 text-[10px] text-neutral-400">{call.durationMs} ms</p>
      )}
    </div>
  );
}
