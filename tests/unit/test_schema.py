"""Schema: every table from 04 exists after migrate; event_log is append-only."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tests.integration.test_postgres_auth_security import service_modules

with service_modules("company_os"):
    from app.db.models import ALL_TABLE_NAMES, APPEND_ONLY_TABLES


def _alembic_cfg(db_path: Path) -> Config:
    root = Path(__file__).resolve().parents[2]
    ini = root / "services" / "company_os" / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(root / "services/company_os/app/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    os.environ["COMPANY_OS_DB_URL"] = f"sqlite:///{db_path}"
    # Ensure app imports resolve
    import sys

    cos = str(root / "services" / "company_os")
    if cos not in sys.path:
        sys.path.insert(0, cos)
    return cfg


@pytest.fixture()
def migrated_db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    # Run from company_os so alembic env can import app.*
    root = Path(__file__).resolve().parents[2]
    cos = root / "services" / "company_os"
    old = os.getcwd()
    os.chdir(cos)
    with service_modules("company_os"):
        try:
            cfg = _alembic_cfg(db_path)
            command.upgrade(cfg, "head")
            yield db_path
            command.downgrade(cfg, "base")
            command.upgrade(cfg, "head")
        finally:
            os.chdir(old)


def test_every_table_from_04_exists(migrated_db: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_db}")
    names = set(inspect(engine).get_table_names())
    missing = ALL_TABLE_NAMES - names
    assert not missing, f"missing tables: {sorted(missing)}"


def test_event_log_append_only(migrated_db: Path) -> None:
    assert "event_log" in APPEND_ONLY_TABLES
    engine = create_engine(f"sqlite:///{migrated_db}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO event_log (id, topic, venture_id, actor, payload_json, ts) "
                "VALUES ('evt_1', 't', NULL, 'sys', '{}', CURRENT_TIMESTAMP)"
            )
        )
        with pytest.raises(Exception):
            conn.execute(text("UPDATE event_log SET topic='x' WHERE id='evt_1'"))
        with pytest.raises(Exception):
            conn.execute(text("DELETE FROM event_log WHERE id='evt_1'"))
