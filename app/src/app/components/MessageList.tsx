"use client";

import { useEffect, useRef } from "react";
import type { ChatMessage } from "../types";
import MessageBubble from "./MessageBubble";

export default function MessageList({ messages }: { messages: ChatMessage[] }) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length]);

  return (
    <div
      aria-live="polite"
      className="flex-1 space-y-3 overflow-y-auto px-6 py-4"
    >
      {messages.length === 0 ? (
        <p className="mt-10 text-center text-sm text-slate-400">
          Say hi, jot a note, or ask what&rsquo;s on your list.
        </p>
      ) : (
        messages.map((m) => <MessageBubble key={m.id} message={m} />)
      )}
      <div ref={endRef} />
    </div>
  );
}
