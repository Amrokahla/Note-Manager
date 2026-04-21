from __future__ import annotations

import numpy as np
import pytest

from backend.services import embeddings


class _FakeClient:
    def __init__(self, vec: list[float]):
        self.vec = vec
        self.last_prompt: str | None = None

    def embeddings(self, *, model: str, prompt: str):
        self.last_prompt = prompt
        return {"embedding": self.vec}


@pytest.fixture
def fake_client(monkeypatch):
    f = _FakeClient([3.0, 4.0, 0.0])  # length-5 after normalization → expect (0.6, 0.8, 0)
    monkeypatch.setattr(embeddings, "_client", f)
    return f


def test_embed_normalizes_vector(fake_client):
    v = embeddings.embed("hello world")
    # Unit norm after normalization
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-6
    # Check the normalized components
    np.testing.assert_allclose(v, np.array([0.6, 0.8, 0.0], dtype=np.float32), atol=1e-6)


def test_embed_rejects_empty(fake_client):
    with pytest.raises(ValueError):
        embeddings.embed("")
    with pytest.raises(ValueError):
        embeddings.embed("   ")


def test_embed_raises_on_zero_vector(monkeypatch):
    zeroed = _FakeClient([0.0, 0.0, 0.0])
    monkeypatch.setattr(embeddings, "_client", zeroed)
    with pytest.raises(RuntimeError, match="zero vector"):
        embeddings.embed("anything")


def test_to_blob_from_blob_roundtrip():
    v = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
    blob = embeddings.to_blob(v)
    assert isinstance(blob, bytes)
    back = embeddings.from_blob(blob)
    np.testing.assert_array_equal(v, back)


def test_cosine_on_unit_vectors_is_dot():
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    assert embeddings.cosine(a, b) == 0.0

    c = np.array([1.0, 0.0], dtype=np.float32)
    assert embeddings.cosine(c, c) == pytest.approx(1.0)


def test_cosine_on_non_unit_vectors_falls_back_to_full_formula():
    a = np.array([3.0, 4.0], dtype=np.float32)  # norm = 5
    b = np.array([3.0, 4.0], dtype=np.float32)
    assert embeddings.cosine(a, b) == pytest.approx(1.0)


def test_cosine_on_zero_vector_returns_zero():
    a = np.zeros(3, dtype=np.float32)
    b = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert embeddings.cosine(a, b) == 0.0
