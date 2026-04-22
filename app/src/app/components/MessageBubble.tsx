"use client";

import type { ChatMessage } from "../types";

export default function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={
          isUser
            ? "max-w-[75%] rounded-2xl rounded-br-md border border-[color:var(--color-petrol)]/20 bg-[color:var(--color-petrol-soft)] px-4 py-2 text-sm text-slate-900 whitespace-pre-wrap"
            : "max-w-[85%] rounded-2xl rounded-bl-md bg-slate-100 px-4 py-2 text-sm text-slate-900 whitespace-pre-wrap"
        }
      >
        {message.content}
      </div>
    </div>
  );
}
