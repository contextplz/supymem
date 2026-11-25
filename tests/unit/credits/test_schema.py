"""Schema invariants: CHECK constraints, append-only triggers, UNIQUE keys, seeds."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_postgres_auth_security import service_modules

with service_modules("company_os"):
    from app.db.models import CREDITS_TABLE_NAMES

_ZERO_HASH = "0" * 64


def _alembic_cfg(db_path: Path) -> Config:
    root = Path(__file__).resolve().parents[3]
    ini = root / "services" / "company_os" / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option(
        "script_location", str(root / "services/company_os/app/db/migrations")
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    os.environ["COMPANY_OS_DB_URL"] = f"sqlite:///{db_path}"
    import sys

    cos = str(root / "services" / "company_os")
    if cos not in sys.path:
        sys.path.insert(0, cos)
    return cfg


@pytest.fixture()
def migrated_db(tmp_path: Path):
    db_path = tmp_path / "credits.db"
    root = Path(__file__).resolve().parents[3]
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


def _engine(migrated_db: Path):
    return create_engine(f"sqlite:///{migrated_db}")


def _insert_account(conn, *, granted: int = 1_000, spent: int = 0, reserved: int = 0) -> None:
    conn.execute(
        text(
            "INSERT INTO credit_accounts ("
            " id, scope, venture_id, granted_micro, spent_micro, reserved_micro,"
            " status, version, created_at, updated_at"
            ") VALUES ("
            " 'cra_1', 'venture', 'ven_1', :granted, :spent, :reserved,"
            " 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"granted": granted, "spent": spent, "reserved": reserved},
    )


def _insert_ledger(conn, *, idempotency_key: str = "seed:1") -> None:
    conn.execute(
        text(
            "INSERT INTO credit_ledger ("
            " id, account_id, kind, amount_micro, available_after_micro, reason,"
            " actor_kind, idempotency_key, prev_hash, entry_hash, created_at"
            ") VALUES ("
            " 'cle_1', 'cra_1', 'grant', 100, 100, 'seed',"
            " 'system', :key, :prev, 'abc', CURRENT_TIMESTAMP)"
        ),
        {"key": idempotency_key, "prev": _ZERO_HASH},
    )


def _insert_audit(conn) -> None:
    conn.execute(
        text(
            "INSERT INTO admin_audit_log ("
            " id, action, outcome, prev_hash, entry_hash, created_at"
            ") VALUES ("
            " 'aal_1', 'read', 'allow', :prev, 'abc', CURRENT_TIMESTAMP)"
        ),
        {"prev": _ZERO_HASH},
    )


def test_credit_tables_exist(migrated_db: Path) -> None:
    names = set(inspect(_engine(migrated_db)).get_table_names())
    missing = CREDITS_TABLE_NAMES - names
    assert not missing, f"missing tables: {sorted(missing)}"


def test_overspend_update_is_rejected(migrated_db: Path) -> None:
    engine = _engine(migrated_db)
    with engine.begin() as conn:
        _insert_account(conn, granted=100, spent=0, reserved=0)
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "UPDATE credit_accounts"
                    " SET spent_micro = 60, reserved_micro = 50"
                    " WHERE id = 'cra_1'"
                )
            )


def test_negative_balances_are_rejected(migrated_db: Path) -> None:
    engine = _engine(migrated_db)
    with engine.begin() as conn:
        with pytest.raises(IntegrityError):
            _insert_account(conn, granted=-1)
        with pytest.raises(IntegrityError):
            _insert_account(conn, granted=100, spent=-1)
        with pytest.raises(IntegrityError):
            _insert_account(conn, granted=100, reserved=-1)


def test_credit_ledger_update_and_delete_raise(migrated_db: Path) -> None:
    engine = _engine(migrated_db)
    with engine.begin() as conn:
        _insert_ledger(conn)
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("UPDATE credit_ledger SET reason = 'x' WHERE id = 'cle_1'"))
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("DELETE FROM credit_ledger WHERE id = 'cle_1'"))


def test_admin_audit_log_update_and_delete_raise(migrated_db: Path) -> None:
    engine = _engine(migrated_db)
    with engine.begin() as conn:
        _insert_audit(conn)
        with pytest.raises(Exception, match="append-only"):
            conn.execute(
                text("UPDATE admin_audit_log SET action = 'x' WHERE id = 'aal_1'")
            )
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("DELETE FROM admin_audit_log WHERE id = 'aal_1'"))


def test_duplicate_idempotency_key_raises(migrated_db: Path) -> None:
    engine = _engine(migrated_db)
    with engine.begin() as conn:
        _insert_ledger(conn, idempotency_key="grant:once")
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO credit_ledger ("
                    " id, account_id, kind, amount_micro, available_after_micro,"
                    " reason, actor_kind, idempotency_key, prev_hash, entry_hash,"
                    " created_at"
                    ") VALUES ("
                    " 'cle_2', 'cra_1', 'grant', 100, 100, 'replay',"
                    " 'system', 'grant:once', :prev, 'def', CURRENT_TIMESTAMP)"
                ),
                {"prev": _ZERO_HASH},
            )


def test_duplicate_provider_event_id_raises(migrated_db: Path) -> None:
    engine = _engine(migrated_db)
    insert = text(
        "INSERT INTO payment_events ("
        " id, provider, provider_event_id, type, status, created_at"
        ") VALUES (:id, 'stripe', 'evt_1', 'checkout.session.completed',"
        " 'received', CURRENT_TIMESTAMP)"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"id": "pay_1"})
        with pytest.raises(IntegrityError):
            conn.execute(insert, {"id": "pay_2"})


def test_duplicate_subject_raises(migrated_db: Path) -> None:
    engine = _engine(migrated_db)
    insert = text(
        "INSERT INTO person_identities (id, person_id, subject, email, created_at)"
        " VALUES (:id, 'per_local000000', 'usr_dup', 'a@b.com', CURRENT_TIMESTAMP)"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"id": "pid_1"})
        with pytest.raises(IntegrityError):
            conn.execute(insert, {"id": "pid_2"})


def test_seeded_policies(migrated_db: Path) -> None:
    engine = _engine(migrated_db)
    with engine.connect() as conn:
        rows = {
            key: json.loads(value)
            for key, value in conn.execute(
                text("SELECT key, value_json FROM credit_policies")
            )
        }
    assert rows["default_venture_credits"] == 5000
    assert rows["max_grant_credits_support"] == 2000
    assert rows["max_grant_credits_finance"] == 5000
    assert rows["dual_control_threshold"] == 50000
    assert rows["reservation_ttl_seconds"] == 900
    assert rows["web_search_credits"] == 2
    assert rows["web_fetch_credits"] == 1
    assert rows["enforcement_enabled"] is True
