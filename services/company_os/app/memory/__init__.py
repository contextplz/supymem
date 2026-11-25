"""Memory package entry — factory + port accessors (doc 72)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from app.config import REPO_ROOT
from app.memory.adapters.local import LocalMemoryAdapter
from app.memory.model import (
    ContextPack,
    GroundedAnswer,
    MemoryHealth,
    MemoryHit,
    MemoryKind,
    MemoryStats,
    MemoryWrite,
    RecallQuery,
    TaskRef,
)
from app.memory.port import MemoryPort
from app.memory.working import WorkingMemory, get_working_memory

__all__ = [
    "ContextPack",
    "GroundedAnswer",
    "MemoryHealth",
    "MemoryHit",
    "MemoryKind",
    "MemoryPort",
    "MemoryStats",
    "MemoryWrite",
    "RecallQuery",
    "TaskRef",
    "WorkingMemory",
    "get_memory",
    "get_working_memory",
    "load_memory_config",
    "reset_memory_for_tests",
]


def load_memory_config() -> dict[str, Any]:
    path = REPO_ROOT / "config" / "memory.yaml"
    if not path.exists():
        return {"memory": {"tier2": "local"}}
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


_ADAPTER: MemoryPort | None = None


def get_memory() -> MemoryPort:
    global _ADAPTER
    if _ADAPTER is None:
        cfg = load_memory_config().get("memory") or {}
        from app.db.session import resolve_database_url
        url = resolve_database_url() or ""
        # Hosted PostgreSQL never silently falls back to a replica-local SQLite file.
        tier2 = "postgres" if url.startswith(("postgres:", "postgresql:" , "postgresql+")) else os.environ.get("COMPANY_OS_MEMORY_ADAPTER") or cfg.get("tier2") or "local"
        if tier2 == "local":
            _ADAPTER = LocalMemoryAdapter()
        elif tier2 == "supymem":
            from app.memory.adapters.supymem import SupymemAdapter

            _ADAPTER = SupymemAdapter()
        elif tier2 == "postgres":
            from app.memory.adapters.postgres import PostgresMemoryAdapter
            _ADAPTER = PostgresMemoryAdapter()
        else:
            raise ValueError(f"unknown memory adapter: {tier2}")
    return _ADAPTER


def reset_memory_for_tests(path: Path | None = None) -> MemoryPort:
    global _ADAPTER
    if _ADAPTER is not None and hasattr(_ADAPTER, "close"):
        try:
            _ADAPTER.close()  # type: ignore[attr-defined]
        except Exception:
            pass
    _ADAPTER = LocalMemoryAdapter(path=path)
    return _ADAPTER
