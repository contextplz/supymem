"""The gateway reuses one users-DB pool per process; a pool per request exhausted RDS."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_pg_engine_is_built_once_per_url(monkeypatch, tmp_path, isolate_app_imports):
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(ROOT / "services/gateway"))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    users, users_pg = importlib.import_module("app.users"), importlib.import_module("app.users_pg")
    built, copied = [], []
    monkeypatch.setattr(users_pg, "_engine", lambda url: built.append(url) or object())
    monkeypatch.setattr(users_pg, "maybe_copy_from_sqlite", copied.append)
    users._pg_engine_for.cache_clear()
    monkeypatch.setenv("GATEWAY_USERS_DB", "postgresql://u@db-a/x")
    first = users._pg_engine()
    assert users._pg_engine() is first and users._pg_engine() is first
    monkeypatch.setenv("GATEWAY_USERS_DB", "postgresql://u@db-b/x")
    assert users._pg_engine() is not first
    assert built == ["postgresql://u@db-a/x", "postgresql://u@db-b/x"] and len(copied) == 2
    users._pg_engine_for.cache_clear()
