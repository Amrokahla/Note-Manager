from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# Normalized types shared by every LLM provider implementation. The orchestrator
# only ever sees these — no provider-specific shapes leak out.

class ToolCall(BaseModel):
    name: str
    arguments: dict


class LLMResponse(BaseModel):
    kind: Literal["tool_calls", "message"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: dict | None = None
