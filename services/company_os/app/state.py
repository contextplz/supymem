"""Shared process state for Company OS routes (working memory + DB factory).

MEM used to be a process-local dict. It is now Tier-1 WorkingMemory (SQLite),
dict-compatible, so venture working state survives process restarts (doc 72 §9).
"""

from __future__ import annotations

from contextlib import nullcontext
from threading import RLock

from app.config import settings
from app.core.local_mode import is_local
from app.db.append_only import install_append_only_triggers
from app.db.session import (
    make_engine,
    make_session_factory,
    normalize_db_url,
    resolve_database_url,
)
from app.memory.working import WorkingMemory, get_working_memory

_engine = None
_factory = None
# ponytail: one process-wide DB factory, per-database locks if parallel app instances are needed.
_db_lock = RLock()

# Durable Tier-1 store. Keep the name MEM for call-site compatibility (S1.5).
MEM: WorkingMemory = get_working_memory()


def reset_db_factory() -> None:
    global _engine, _factory
    with _db_lock:
        _engine = None
        _factory = None


def db():
    global _engine, _factory
    with _db_lock:
        if _factory is None:
            url = resolve_database_url() or settings.db_url
            url = normalize_db_url(url)
            _engine = make_engine(url)
            from app.db.models import Base
            from app.community.schema import ensure_community_schema
            from app.gtm.supymem.schema import ensure_supymem_schema
            from app.marketing.media.schema import ensure_marketing_media_schema
            from app.inbox.store import ensure_schema as ensure_inbox_schema
            from app.coding_agents_consent import ensure_schema as ensure_coding_consent_schema

            lock = nullcontext()
            if is_local():
                from app.db.local_sqlite import sqlite_schema_lock

                lock = sqlite_schema_lock(url)
            with lock:
                Base.metadata.create_all(_engine)
                ensure_supymem_schema(_engine)
                ensure_marketing_media_schema(_engine)
                ensure_community_schema(_engine)
                ensure_inbox_schema(_engine)
                ensure_coding_consent_schema(_engine)
                install_append_only_triggers(_engine)
            _factory = make_session_factory(_engine)
        return _factory


def get_factory():
    return _factory


def set_factory(factory) -> None:
    global _factory
    with _db_lock:
        _factory = factory


def reset_mem_for_tests(path=None) -> WorkingMemory:
    """Test helper — rebind MEM to a fresh WorkingMemory."""
    global MEM
    from app.memory.working import reset_working_memory_for_tests

    MEM = reset_working_memory_for_tests(path)
    return MEM
