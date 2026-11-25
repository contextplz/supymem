from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def money_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/summary.db")
    load_company_os_app()
    from app import state
    from app.db.models import Venture
    from core.clock import utcnow

    state.reset_db_factory()
    now = utcnow()
    with state.db()() as session:
        session.add(Venture(id="ven_summary", workspace_id=None, name="Summary", raw_idea="Summary test venture", stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.commit()


def test_no_data_is_not_fabricated_as_live_or_zero(money_env) -> None:
    from app.money.summary import money_summary

    result = money_summary("ven_summary")
    assert result["mode"] == "no_data"
    assert result["earned_minor"] is None
    assert result["provenance"]["ledger_entries"] == 0


def test_an_unverified_stripe_label_does_not_establish_live_revenue(money_env) -> None:
    from app.money.ledger import record_entry
    from app.money.summary import money_summary

    record_entry("ven_summary", "cash:stripe", "revenue", 999, "INR", "evt_1", {"source": "stripe"})
    result = money_summary("ven_summary")
    assert result["mode"] == "no_data"
    assert result["earned_minor"] is None
    assert result["provenance"]["excluded_entries"] == 1
