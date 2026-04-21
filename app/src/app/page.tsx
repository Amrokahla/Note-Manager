"use client";

import { useReducer } from "react";
import ChatPanel from "./components/ChatPanel";
import Header from "./components/Header";
import ToolPanel from "./components/ToolPanel";
import { appReducer, initialState } from "./lib/reducer";

export default function Home() {
  const [state, dispatch] = useReducer(appReducer, undefined, initialState);

  return (
    <main className="flex h-dvh flex-col">
      <Header
        sessionId={state.sessionId}
        onReset={() => dispatch({ type: "RESET" })}
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[7fr_3fr]">
        <ChatPanel state={state} dispatch={dispatch} />
        <ToolPanel toolCalls={state.toolCalls} />
      </div>
    </main>
  );
}
