"""Alembic + API honour DATABASE_URL (existing ECS/RDS name)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SESSION_PY = ROOT / "services" / "company_os" / "app" / "db" / "session.py"


def _load_session():
    name = "company_os_db_session_under_test"
    spec = importlib.util.spec_from_file_location(name, SESSION_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_resolve_database_url_honour_order():
    resolve = _load_session().resolve_database_url
    assert (
        resolve(
            {
                "COMPANY_OS_DB_URL": "postgresql://cos:cos@db/companyos",
                "DATABASE_URL": "postgresql://other/other",
                "DB_URL": "sqlite:///ignored.db",
            }
        )
        == "postgresql://cos:cos@db/companyos"
    )
    assert resolve({"DATABASE_URL": "postgresql://rds/supymem"}) == "postgresql://rds/supymem"
    assert resolve({"DB_URL": "sqlite:///only.db"}) == "sqlite:///only.db"
    assert resolve({}) == ""
    assert resolve({"DATABASE_URL": "  "}) == ""


def test_alembic_env_calls_resolve_database_url():
    env = (
        ROOT / "services" / "company_os" / "app" / "db" / "migrations" / "env.py"
    ).read_text(encoding="utf-8")
    assert "resolve_database_url" in env
    assert "DATABASE_URL" in env
    # Must not skip DATABASE_URL (the live ECS name).
    assert "os.environ.get(\"COMPANY_OS_DB_URL\") or os.environ.get(\"DB_URL\")" not in env


def test_state_db_factory_uses_resolve_database_url():
    src = (ROOT / "services" / "company_os" / "app" / "state.py").read_text(encoding="utf-8")
    assert "resolve_database_url" in src


@pytest.mark.parametrize("domain", ["fleet-maintenance", "home-baking"])
def test_source_database_keeps_existing_files_and_requires_an_explicit_choice(tmp_path, domain):
    directory = tmp_path / domain
    directory.mkdir()
    resolve = _load_session().resolve_source_database_url
    legacy, desktop = directory / "local.db", directory / "database.sqlite3"
    assert resolve({}, directory) == f"sqlite:///{legacy}"
    assert not legacy.exists()
    desktop.write_text("existing database")
    assert resolve({}, directory) == f"sqlite:///{desktop}"
    with pytest.raises(ValueError, match="already has a database"):
        resolve({"DB_URL": f"sqlite:///{legacy}"}, directory)
    legacy.write_text("different existing database")
    with pytest.raises(ValueError, match="COMPANY_OS_DB_URL"):
        resolve({}, directory)
    assert resolve({"DB_URL": f"sqlite+aiosqlite:///{legacy}"}, directory) == f"sqlite:///{legacy}"
    with pytest.raises(ValueError, match="DATABASE_URL"):
        resolve({"COMPANY_OS_DB_URL": f"sqlite:///{legacy}", "DATABASE_URL": f"sqlite:///{desktop}"}, directory)
    with pytest.raises(ValueError, match="COMPANY_OS_DATA_DIR"):
        resolve({"DB_URL": f"sqlite:///{tmp_path / 'other.db'}"}, directory)
    assert desktop.read_text() == "existing database" and legacy.read_text() == "different existing database"


@pytest.mark.parametrize("domain", ["fleet-maintenance", "home-baking"])
@pytest.mark.parametrize("ambient_mode", [None, "local"])
def test_resolve_database_url_uses_supplied_environment(monkeypatch, tmp_path, isolate_app_imports, domain, ambient_mode):
    monkeypatch.syspath_prepend(str(ROOT / "services" / "company_os"))
    for name in ("COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL"):
        monkeypatch.delenv(name, raising=False)
    if ambient_mode is None:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    else:
        monkeypatch.setenv("SUPYMEM_MODE", ambient_mode)
    directory = tmp_path / domain
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(directory))
    resolve = _load_session().resolve_database_url
    assert resolve({}) == ""
    assert resolve({"SUPYMEM_MODE": "hosted", "COMPANY_OS_DATA_DIR": str(directory)}) == ""
    local_url = f"sqlite:///{directory / 'database.sqlite3'}"
    assert resolve({"SUPYMEM_MODE": " LOCAL ", "COMPANY_OS_DATA_DIR": str(directory)}) == local_url
    assert resolve() == (local_url if ambient_mode == "local" else "")
    assert not (directory / "database.sqlite3").exists()
