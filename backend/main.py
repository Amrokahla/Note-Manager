from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from ollama import Client

from backend.config import settings
from backend.db.sqlite import init_db

ollama = Client(host=settings.ollama_host)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Note Agent", lifespan=lifespan)


def _model_is_available(list_response, target: str) -> bool:
    raw = list_response.model_dump() if hasattr(list_response, "model_dump") else list_response
    for m in raw.get("models", []):
        name = m.get("model") or m.get("name") or ""
        if target in name:
            return True
    return False


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
