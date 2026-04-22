from __future__ import annotations

import logging
from typing import Callable

from backend.agent import llm_gemini, llm_ollama
from backend.agent.llm_types import LLMResponse, ToolCall

__all__ = ["chat", "ToolCall", "LLMResponse", "MODEL_OPTIONS", "DEFAULT_MODEL"]

logger = logging.getLogger(__name__)


MODEL_OPTIONS: dict[str, tuple[str, str | None]] = {
    "ollama":           ("ollama", None),
    "ollama-llama3.2":  ("ollama", "llama3.2"),
    "gemini-2.5-pro":   ("gemini", "gemini-2.5-pro"),
    "gemini-2.5-flash": ("gemini", "gemini-2.5-flash"),
}

DEFAULT_MODEL = "ollama"


def chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    on_delta: Callable[[str], None] | None = None,
    model: str = DEFAULT_MODEL,
) -> LLMResponse:
    """Dispatch a chat request to the provider bound to `model` in MODEL_OPTIONS."""
    provider, concrete_model = MODEL_OPTIONS.get(model, ("ollama", None))

    if provider == "gemini":
        assert concrete_model is not None, "Gemini routing requires a concrete model id"
        return llm_gemini.chat(
            messages,
            model=concrete_model,
            tools=tools,
            on_delta=on_delta,
        )

    return llm_ollama.chat(
        messages, tools=tools, on_delta=on_delta, model=concrete_model
    )
