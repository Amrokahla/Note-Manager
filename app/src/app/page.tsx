"use client";

import { useReducer } from "react";
import ChatPanel from "./components/ChatPanel";
import Header from "./components/Header";
import ToolPanel from "./components/ToolPanel";
import { mockInitialState } from "./lib/mockData";
import { appReducer } from "./lib/reducer";
import { newSessionId } from "./lib/session";

// F1: seed the reducer with mock data so every UI state is visible before the
// real /chat wiring lands in F2. Swap the initializer to `initialState()` from
// ./lib/reducer when F2 comes online; the reducer itself is already wired for
// live events.
function seedInitialState() {
  return mockInitialState(newSessionId());
}

export default function Home() {
  const [state, dispatch] = useReducer(appReducer, undefined, seedInitialState);

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
