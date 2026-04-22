from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Note(BaseModel):
    id: int
    title: str
    description: str
    tag: str | None = None
    created_at: datetime
    updated_at: datetime


class NoteSummary(BaseModel):
    """Compact form for list and search results."""

    id: int
    title: str
    description: str
    tag: str | None = None
    updated_at: datetime
    similarity: float | None = None


class TagCount(BaseModel):
    tag: str
    count: int
