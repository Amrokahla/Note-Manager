from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Note(BaseModel):
    id: int
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class NoteSummary(BaseModel):
    """Compact form used for search results and candidate lists."""

    id: int
    title: str
    snippet: str
    tags: list[str] = Field(default_factory=list)
    updated_at: datetime
