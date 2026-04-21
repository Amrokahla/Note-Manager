from __future__ import annotations

import logging

import numpy as np
from ollama import Client

from backend.config import settings

logger = logging.getLogger(__name__)

# nomic-embed-text emits 768-dim vectors. We normalize at write/query time so
# cosine similarity is just a dot product — keeps the search loop tight.

_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(host=settings.ollama_host)
    return _client


def embed(text: str) -> np.ndarray:
    """Return a unit-norm float32 embedding for `text`.

    Raises on Ollama error — the caller (note_service) surfaces that as a
    ToolResult so the LLM can tell the user something's wrong.
    """
    if not text or not text.strip():
        raise ValueError("Cannot embed empty text")

    client = _get_client()
    resp = client.embeddings(model=settings.ollama_embed_model, prompt=text)
    data = resp.model_dump() if hasattr(resp, "model_dump") else resp
    vec = np.asarray(data["embedding"], dtype=np.float32)

    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        raise RuntimeError("Embedding model returned a zero vector")
    return vec / norm


def to_blob(vec: np.ndarray) -> bytes:
    """Pack a vector into SQLite-friendly bytes."""
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    """Reconstruct a vector from BLOB bytes."""
    return np.frombuffer(blob, dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity. Assumes both vectors are unit-norm (our write path
    ensures this). Falls back to full formula if either has non-unit norm."""
    dot = float(np.dot(a, b))
    # Cheap guard in case a bad input slipped through.
    a_norm = float(np.linalg.norm(a))
    b_norm = float(np.linalg.norm(b))
    if a_norm == 0.0 or b_norm == 0.0:
        return 0.0
    if abs(a_norm - 1.0) < 1e-5 and abs(b_norm - 1.0) < 1e-5:
        return dot
    return dot / (a_norm * b_norm)
