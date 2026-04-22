"use client";

import type { Dispatch } from "react";
import { useRef } from "react";
import { sendMessage } from "../lib/api";
import type { Action } from "../lib/reducer";
import type { AppState } from "../types";
import Composer from "./Composer";
import ErrorBanner from "./ErrorBanner";
import MessageList from "./MessageList";

interface Props {
  state: AppState;
  dispatch: Dispatch<Action>;
}

export default function ChatPanel({ state, dispatch }: Props) {
  const lastSent = useRef<string | null>(null);

  async function submit(text: string) {
    if (!state.sessionId) return;

    const turnId = crypto.randomUUID();
    lastSent.current = text;

    dispatch({ type: "USER_MESSAGE", content: text, turnId });
    dispatch({ type: "STREAM_START" });

    await sendMessage(state.sessionId, text, turnId, state.model, {
      onUserEcho: () => {},
      onToolCall: (call) => dispatch({ type: "TOOL_CALL_START", call }),
      onToolResult: (r) =>
        dispatch({
          type: "TOOL_CALL_RESULT",
          id: r.id,
          status: r.status,
          message: r.message,
          errorCode: r.errorCode,
        }),
      onAssistantDelta: (delta) =>
        dispatch({ type: "ASSISTANT_DELTA", content: delta, turnId }),
      onAssistant: (content) =>
        dispatch({ type: "ASSISTANT_MESSAGE", content, turnId }),
      onStreamDrop: () => dispatch({ type: "STREAM_DROP" }),
      onDone: () => dispatch({ type: "STREAM_END" }),
      onError: (message) => dispatch({ type: "ERROR", message }),
    });
  }

  function handleRetry() {
    if (state.isStreaming) return;
    const text = lastSent.current;
    if (!text) return;
    dispatch({ type: "DISMISS_ERROR" });
    submit(text);
  }

  return (
    <section className="flex min-h-0 flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      <MessageList messages={state.messages} />
      {state.error && (
        <ErrorBanner
          message={state.error}
          canRetry={!!lastSent.current && !state.isStreaming}
          onRetry={handleRetry}
          onDismiss={() => dispatch({ type: "DISMISS_ERROR" })}
        />
      )}
      <Composer disabled={state.isStreaming} onSubmit={submit} />
    </section>
  );
}
