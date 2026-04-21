import type { AppState } from "../types";

// F1 fixture: one user turn that exercises every tool-call status the UI needs
// to render. Lets us build components before the backend `/chat` is wired.
export function mockInitialState(sessionId: string): AppState {
  const now = Date.now();
  const turnA = "turn-a";
  const turnB = "turn-b";

  return {
    sessionId,
    isStreaming: false,
    error: undefined,
    messages: [
      {
        id: "m1",
        role: "user",
        content: "Add a note about the standup — we moved it to Tuesdays.",
        createdAt: now - 60_000,
        turnId: turnA,
      },
      {
        id: "m2",
        role: "assistant",
        content: "Saved! Note #17 — tagged `meetings`.",
        createdAt: now - 58_000,
        turnId: turnA,
      },
      {
        id: "m3",
        role: "user",
        content: "Delete the old office note.",
        createdAt: now - 20_000,
        turnId: turnB,
      },
      {
        id: "m4",
        role: "assistant",
        content:
          'About to delete note #8 "Old office address". Do you want me to go ahead?',
        createdAt: now - 18_000,
        turnId: turnB,
      },
    ],
    toolCalls: [
      {
        id: "tc1",
        turnId: turnA,
        name: "add_note",
        arguments: {
          title: "standup",
          body: "moved to Tuesdays",
          tags: ["meetings"],
        },
        status: "ok",
        message: "Created note #17.",
        startedAt: now - 59_500,
        endedAt: now - 59_100,
        durationMs: 400,
      },
      {
        id: "tc2",
        turnId: turnB,
        name: "search_notes",
        arguments: { query: "old office", limit: 1 },
        status: "fail",
        message: "No notes matched.",
        errorCode: "not_found",
        startedAt: now - 19_500,
        endedAt: now - 19_200,
        durationMs: 300,
      },
      {
        id: "tc3",
        turnId: turnB,
        name: "delete_note",
        arguments: { note_id: 8 },
        status: "needs_confirmation",
        message: "Please confirm with the user before proceeding.",
        startedAt: now - 18_800,
        endedAt: now - 18_400,
        durationMs: 400,
      },
    ],
  };
}
