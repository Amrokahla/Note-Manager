from __future__ import annotations

import json
import logging
import queue
import threading
from contextlib import asynccontextmanager
from typing import Any, Iterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from ollama import Client
from pydantic import BaseModel, Field

from backend.agent import intent_parser
from backend.agent.llm_handler import DEFAULT_MODEL, MODEL_OPTIONS
from backend.config import settings
from backend.db.sqlite import init_db
from backend.services import note_service

logger = logging.getLogger(__name__)

ollama = Client(host=settings.ollama_host)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        note_service.backfill_embeddings()
    except Exception as e:
        logger.warning("Startup backfill skipped: %s", e)
    yield


app = FastAPI(title="Note Agent", lifespan=lifespan)

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
    """Friendly pointer; the UI lives on the Next.js app at :3000."""
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


class ChatIn(BaseModel):
    session_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    model: str = Field(default=DEFAULT_MODEL)


class ChatToolCall(BaseModel):
    id: str
    name: str
    arguments: dict
    result: dict


class ChatOut(BaseModel):
    session_id: str
    reply: str
    tool_calls: list[ChatToolCall]


def _resolved_model(requested: str) -> str:
    return requested if requested in MODEL_OPTIONS else DEFAULT_MODEL


@app.get("/models")
def models():
    """Allowed model ids so the UI can render a selector."""
    return {
        "default": DEFAULT_MODEL,
        "options": list(MODEL_OPTIONS.keys()),
    }


@app.post("/chat", response_model=ChatOut)
def chat(body: ChatIn) -> ChatOut:
    turn = intent_parser.handle_user_message(
        body.session_id, body.message, model=_resolved_model(body.model)
    )
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


def _format_sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


def _sse_stream(session_id: str, message: str, model: str) -> Iterator[str]:
    q: queue.Queue[tuple[str, dict] | object] = queue.Queue()
    sentinel = object()

    def emit(event_type: str, data: dict) -> None:
        q.put((event_type, data))

    def run_orchestrator() -> None:
        try:
            intent_parser.handle_user_message(
                session_id, message, emit=emit, model=model
            )
        except Exception as e:
            logger.exception("Orchestrator crashed while streaming")
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            q.put(sentinel)

    threading.Thread(target=run_orchestrator, daemon=True).start()

    while True:
        item = q.get()
        if item is sentinel:
            break
        event_type, data = item  # type: ignore[misc]
        yield _format_sse(event_type, data)


@app.post("/chat/stream")
def chat_stream(body: ChatIn) -> StreamingResponse:
    return StreamingResponse(
        _sse_stream(body.session_id, body.message, _resolved_model(body.model)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
