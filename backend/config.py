from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.2")
    db_path: str = os.getenv("DB_PATH", "./data/notes.db")
    max_tool_hops: int = int(os.getenv("MAX_TOOL_HOPS", "5"))
    history_turns: int = int(os.getenv("HISTORY_TURNS", "20"))


settings = Settings()
