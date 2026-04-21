from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from ollama import Client
from pydantic import BaseModel, Field

from backend.agent import intent_parser
from backend.config import settings
from backend.db.sqlite import init_db

ollama = Client(host=settings.ollama_host)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Note Agent", lifespan=lifespan)

# CORS: the Next.js dev server runs on :3000 (or :3001 if occupied) and needs
# to hit this API on :8000. Production deployment would tighten allow_origins
# to the real host list.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _model_is_available(list_response: Any, target: str) -> bool:
    raw = list_response.model_dump() if hasattr(list_response, "model_dump") else list_response
    for m in raw.get("models", []):
        name = m.get("model") or m.get("name") or ""
        if target in name:
            return True
    return False


@app.get("/")
def root():
    """Friendly pointer so hitting :8000 directly doesn't look broken.
    The UI lives in the separate Next.js app on :3000."""
    return {
        "service": "Note Agent API",
        "ui": "http://localhost:3000",
        "docs": "/docs",
        "endpoints": ["/health", "/chat"],
    }


@app.get("/health")
def health():
    try:
        resp = ollama.list()
        return {
            "ok": _model_is_available(resp, settings.ollama_model),
            "model": settings.ollama_model,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "model": settings.ollama_model}


# --- POST /chat --------------------------------------------------------------
#
# Non-streaming endpoint consumed by the Next.js UI. Shape matches
# FRONTEND_PLAN §4.1 exactly: { reply, tool_calls[{id, name, arguments, result}] }.
# SSE streaming (FRONTEND_PLAN §4.2) is deferred until frontend F3.

class ChatIn(BaseModel):
    session_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)


class ChatToolCall(BaseModel):
    id: str
    name: str
    arguments: dict
    result: dict


class ChatOut(BaseModel):
    session_id: str
    reply: str
    tool_calls: list[ChatToolCall]


@app.post("/chat", response_model=ChatOut)
def chat(body: ChatIn) -> ChatOut:
    turn = intent_parser.handle_user_message(body.session_id, body.message)
    return ChatOut(
        session_id=body.session_id,
        reply=turn.reply,
        tool_calls=[
            ChatToolCall(
                id=tc.id,
                name=tc.name,
                arguments=tc.arguments,
                result=tc.result.model_dump(mode="json"),
            )
            for tc in turn.tool_calls
        ],
    )
