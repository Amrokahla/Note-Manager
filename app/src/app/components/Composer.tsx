"use client";

import { Send } from "lucide-react";
import { useState } from "react";

interface Props {
  disabled?: boolean;
  onSubmit: (text: string) => void;
}

export default function Composer({ disabled, onSubmit }: Props) {
  const [value, setValue] = useState("");
  const canSend = value.trim().length > 0 && !disabled;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSend) return;
    onSubmit(value.trim());
    setValue("");
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="border-t border-neutral-200 bg-white px-6 py-4"
    >
      <label htmlFor="composer-input" className="sr-only">
        Message
      </label>
      <div className="flex items-center gap-2">
        <input
          id="composer-input"
          type="text"
          autoComplete="off"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Talk to your notes…"
          disabled={disabled}
          className="flex-1 rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 placeholder-neutral-400 focus:border-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-200 disabled:bg-neutral-50"
        />
        <button
          type="submit"
          disabled={!canSend}
          className="inline-flex items-center gap-1 rounded-md bg-indigo-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-neutral-300"
        >
          <Send size={14} />
          Send
        </button>
      </div>
    </form>
  );
}
