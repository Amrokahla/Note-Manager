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

  useEffect(() => {
    if (!state.sessionId) {
      dispatch({ type: "INIT_SESSION", sessionId: newSessionId() });
    }
  }, [state.sessionId]);

  useEffect(() => {
    const saved = loadModel();
    if (saved !== state.model) {
      dispatch({ type: "SET_MODEL", model: saved });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleModelChange(next: ModelId) {
    saveModel(next);
    dispatch({ type: "SET_MODEL", model: next });
  }

  return (
    <main className="flex h-dvh flex-col bg-slate-50 px-[5%]">
      <Header
        sessionId={state.sessionId}
        model={state.model}
        isStreaming={state.isStreaming}
        onReset={() => dispatch({ type: "RESET" })}
        onModelChange={handleModelChange}
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-x-4 py-4 lg:grid-cols-[7fr_2fr]">
        <ChatPanel state={state} dispatch={dispatch} />
        <ToolPanel toolCalls={state.toolCalls} />
      </div>
    </main>
  );
}
