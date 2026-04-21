"use client";

import type { Dispatch } from "react";
import type { Action } from "../lib/reducer";
import type { AppState } from "../types";
import Composer from "./Composer";
import MessageList from "./MessageList";

interface Props {
  state: AppState;
  dispatch: Dispatch<Action>;
}

export default function ChatPanel({ state, dispatch }: Props) {
  function handleSubmit(text: string) {
    // F1: just echo the user turn locally so we can see it flow into the
    // list. The real /chat call lands in F2.
    const turnId = crypto.randomUUID();
    dispatch({ type: "USER_MESSAGE", content: text, turnId });
  }

  return (
    <section className="flex min-h-0 flex-col">
      <MessageList messages={state.messages} />
      {state.error && (
        <div
          role="alert"
          className="border-t border-rose-200 bg-rose-50 px-6 py-2 text-xs text-rose-700"
        >
          {state.error}
        </div>
      )}
      <Composer disabled={state.isStreaming} onSubmit={handleSubmit} />
    </section>
  );
}
