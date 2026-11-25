from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def money_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/money.db")
    load_company_os_app()
    from app import state
    from app.db.models import Venture
    from core.clock import utcnow

    state.reset_db_factory()
    now = utcnow()
    with state.db()() as session:
        session.add(
            Venture(
                id="ven_money",
                workspace_id=None,
                name="Money",
                raw_idea="Money test venture",
                stage="draft",
                autonomy_level="L1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def test_records_balanced_entry_idempotently(money_env) -> None:
    from app.money.ledger import record_entry

    first = record_entry(
        "ven_money", "cash:stripe", "revenue", 49900, "inr", "pi_123", {"source": "stripe"}
    )
    replay = record_entry(
        "ven_money", "cash:stripe", "revenue", 49900, "INR", "pi_123", {"source": "stripe"}
    )
    assert first.id == replay.id
    assert first.amount_minor == 49900


def test_rejects_invalid_minor_units(money_env) -> None:
    from app.money.ledger import record_entry

    with pytest.raises(ValueError, match="positive integer"):
        record_entry("ven_money", "cash", "revenue", 0, "INR")
