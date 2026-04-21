"use client";

import { useEffect, useReducer } from "react";
import ChatPanel from "./components/ChatPanel";
import Header from "./components/Header";
import ToolPanel from "./components/ToolPanel";
import { loadModel, saveModel } from "./lib/modelStorage";
import { appReducer, initialState } from "./lib/reducer";
import { newSessionId } from "./lib/session";
import type { ModelId } from "./types";

export default function Home() {
  const [state, dispatch] = useReducer(appReducer, undefined, initialState);

  // Generate the session id on the client only — doing it during the reducer's
  // lazy-init would run on the server during SSR and again on hydration with
  // a different value, causing a hydration mismatch.
  useEffect(() => {
    if (!state.sessionId) {
      dispatch({ type: "INIT_SESSION", sessionId: newSessionId() });
    }
  }, [state.sessionId]);

  // Restore the saved model pick from localStorage after mount (same hydration
  // safety constraint — can't read localStorage during the reducer seed).
  useEffect(() => {
    const saved = loadModel();
    if (saved !== state.model) {
      dispatch({ type: "SET_MODEL", model: saved });
    }
    // Only on mount — subsequent SET_MODEL dispatches handle persistence via
    // the handler below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleModelChange(next: ModelId) {
    saveModel(next);
    dispatch({ type: "SET_MODEL", model: next });
  }

  return (
    <main className="flex h-dvh flex-col">
      <Header
        sessionId={state.sessionId}
        model={state.model}
        isStreaming={state.isStreaming}
        onReset={() => dispatch({ type: "RESET" })}
        onModelChange={handleModelChange}
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[7fr_3fr]">
        <ChatPanel state={state} dispatch={dispatch} />
        <ToolPanel toolCalls={state.toolCalls} />
      </div>
    </main>
  );
}
