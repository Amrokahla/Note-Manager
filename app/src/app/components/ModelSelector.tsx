"use client";

import { Cpu } from "lucide-react";
import type { ModelId } from "../types";
import { MODEL_OPTIONS } from "../types";

interface Props {
  value: ModelId;
  disabled?: boolean;
  onChange: (next: ModelId) => void;
}

export default function ModelSelector({ value, disabled, onChange }: Props) {
  return (
    <label className="inline-flex items-center gap-1 rounded-md border border-neutral-200 bg-white px-2 py-1 text-xs text-neutral-700 focus-within:border-indigo-400 focus-within:ring-2 focus-within:ring-indigo-200">
      <Cpu size={12} aria-hidden="true" />
      <span className="sr-only">Model</span>
      <select
        className="bg-transparent pr-1 text-xs font-medium text-neutral-800 outline-none disabled:text-neutral-400"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value as ModelId)}
      >
        {MODEL_OPTIONS.map((opt) => (
          <option key={opt.id} value={opt.id}>
            {opt.label}
          </option>
        ))}
      </select>
    </label>
  );
}
