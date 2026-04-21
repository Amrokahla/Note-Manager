from __future__ import annotations

import logging
from typing import Callable

from backend.agent import llm_gemini, llm_ollama
from backend.agent.llm_types import LLMResponse, ToolCall

__all__ = ["chat", "ToolCall", "LLMResponse", "MODEL_OPTIONS", "DEFAULT_MODEL"]

logger = logging.getLogger(__name__)


# Public model ids the UI can send. Each maps to a (provider, concrete-model).
# When provider="ollama", the concrete model comes from settings.ollama_model
# (so users can swap the local model via env without a code change).

MODEL_OPTIONS: dict[str, tuple[str, str | None]] = {
    "ollama":           ("ollama", None),
    "gemini-2.5-pro":   ("gemini", "gemini-2.5-pro"),
    "gemini-2.5-flash": ("gemini", "gemini-2.5-flash"),
}

# Default for direct Python callers (tests, CLI). The UI always sends an
# explicit model in /chat and /chat/stream, so this default is only a safety
# net for code paths that don't pass `model=`.
DEFAULT_MODEL = "ollama"


def chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    on_delta: Callable[[str], None] | None = None,
    model: str = DEFAULT_MODEL,
) -> LLMResponse:
    """Dispatch a chat request to the selected LLM provider.

    `model` is one of the keys in MODEL_OPTIONS — a single public identifier
    that the UI sends. The dispatcher picks the right provider module;
    everything above this function is provider-agnostic.
    """
    provider, concrete_model = MODEL_OPTIONS.get(model, ("ollama", None))

    if provider == "gemini":
        assert concrete_model is not None, "Gemini routing requires a concrete model id"
        return llm_gemini.chat(
            messages,
            model=concrete_model,
            tools=tools,
            on_delta=on_delta,
        )

    # Default: Ollama.
    return llm_ollama.chat(messages, tools=tools, on_delta=on_delta)
