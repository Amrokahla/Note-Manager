"use client";

import type { Dispatch } from "react";
import { sendMessage } from "../lib/api";
import type { Action } from "../lib/reducer";
import type { AppState } from "../types";
import Composer from "./Composer";
import MessageList from "./MessageList";

interface Props {
  state: AppState;
  dispatch: Dispatch<Action>;
}

export default function ChatPanel({ state, dispatch }: Props) {
  async function handleSubmit(text: string) {
    if (!state.sessionId) return; // wait for the mount-time INIT_SESSION

    const turnId = crypto.randomUUID();

    // Echo the user bubble immediately so the UI feels responsive; then kick
    // off the backend round-trip. F3's SSE path will reuse the same handlers.
    dispatch({ type: "USER_MESSAGE", content: text, turnId });
    dispatch({ type: "STREAM_START" });

    await sendMessage(state.sessionId, text, turnId, {
      onUserEcho: () => {
        /* already echoed above; no-op in F2 */
      },
      onToolCall: (call) => dispatch({ type: "TOOL_CALL_START", call }),
      onToolResult: (r) =>
        dispatch({
          type: "TOOL_CALL_RESULT",
          id: r.id,
          status: r.status,
          message: r.message,
          errorCode: r.errorCode,
        }),
      onAssistant: (content) =>
        dispatch({ type: "ASSISTANT_MESSAGE", content, turnId }),
      onDone: () => dispatch({ type: "STREAM_END" }),
      onError: (message) => dispatch({ type: "ERROR", message }),
    });
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
