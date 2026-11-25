"""Shared DB fixture for credit service tests. Reuses the identity-test pattern."""

from __future__ import annotations
from tests.credit_fixtures import ensure_test_wallet

import json
import sys
import uuid
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app

_COS = Path(__file__).resolve().parents[3] / "services" / "company_os"
if str(_COS) not in sys.path:
    sys.path.insert(0, str(_COS))

_SEEDED_POLICIES: tuple[tuple[str, object], ...] = (
    ("default_venture_credits", 5000),
    ("max_grant_credits_support", 2000),
    ("max_grant_credits_finance", 5000),
    ("dual_control_threshold", 50000),
    ("reservation_ttl_seconds", 900),
    ("web_search_credits", 2),
    ("web_fetch_credits", 1),
    ("enforcement_enabled", True),
    (
        "credit_packs",
        [
            {
                "sku": "cr_500",
                "credits": 500,
                "usd": 500,
                "inr": 44900,
                "label": "Starter",
            },
            {
                "sku": "cr_1000",
                "credits": 1000,
                "usd": 1000,
                "inr": 89900,
                "label": "Builder",
            },
            {
                "sku": "cr_2000",
                "credits": 2000,
                "usd": 2000,
                "inr": 179900,
                "label": "Studio",
                "badge": "Popular",
            },
            {
                "sku": "cr_5000",
                "credits": 5000,
                "usd": 5000,
                "inr": 439900,
                "label": "Scale",
                "bonus_credits": 250,
            },
        ],
    ),
)

_TRIGGER_SQL = (
    """
    CREATE TRIGGER IF NOT EXISTS credit_ledger_no_update
    BEFORE UPDATE ON credit_ledger BEGIN
      SELECT RAISE(ABORT, 'credit_ledger is append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS credit_ledger_no_delete
    BEFORE DELETE ON credit_ledger BEGIN
      SELECT RAISE(ABORT, 'credit_ledger is append-only');
    END;
    """,
)


def _seed_credit_infra() -> None:
    from sqlalchemy import text

    from app.state import db

    factory = db()
    with factory() as session:
        for key, value in _SEEDED_POLICIES:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO credit_policies "
                    "(key, value_json, updated_at, updated_by) "
                    "VALUES (:key, :value_json, CURRENT_TIMESTAMP, 'system')"
                ),
                {"key": key, "value_json": json.dumps(value)},
            )
        for stmt in _TRIGGER_SQL:
            session.execute(text(stmt))
        session.commit()


@pytest.fixture()
def credits_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/credits.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAYMENTS_ACCEPTING", "1")
    load_company_os_app()
    from app import state
    from app.credits.policy import invalidate

    state.reset_db_factory()
    state.db()
    _seed_credit_infra()
    invalidate()
    yield
    invalidate()


@pytest.fixture()
def account(credits_env):
    return ensure_test_wallet(f"ven_{uuid.uuid4().hex[:12]}")
