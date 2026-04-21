"use client";

import { Check, Loader2, ShieldAlert, X } from "lucide-react";
import type { ToolStatus } from "../types";

const STYLES: Record<
  ToolStatus,
  { label: string; className: string; Icon: typeof Check }
> = {
  running: {
    label: "running…",
    className: "bg-neutral-100 text-neutral-600 border-neutral-200",
    Icon: Loader2,
  },
  ok: {
    label: "ok",
    className: "bg-emerald-50 text-emerald-700 border-emerald-200",
    Icon: Check,
  },
  fail: {
    label: "fail",
    className: "bg-rose-50 text-rose-700 border-rose-200",
    Icon: X,
  },
  needs_confirmation: {
    label: "confirm?",
    className: "bg-amber-50 text-amber-700 border-amber-200",
    Icon: ShieldAlert,
  },
};

export default function StatusBadge({ status }: { status: ToolStatus }) {
  const { label, className, Icon } = STYLES[status];
  const spinning = status === "running";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium ${className}`}
    >
      <Icon size={10} className={spinning ? "animate-spin" : undefined} />
      {label}
    </span>
  );
}
