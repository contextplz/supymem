"""Founder-scoped Settings controls: per-venture autonomy, credit request, trial entitlement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

H = {"X-Subject": "founder"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/settings.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.delenv("PAYMENTS_ACCEPTING", raising=False)
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


def _venture(client: TestClient) -> str:
    r = client.post("/ventures", json={"raw_idea": "invoice chaser for freelancers", "start": False})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_founder_sets_autonomy_for_this_venture(client):
    vid = _venture(client)
    r = client.post(f"/ventures/{vid}/autonomy", json={"level": "L2"}, headers=H)
    assert r.status_code == 200, r.text
    assert client.get(f"/ventures/{vid}").json()["autonomy_level"] == "L2"
    assert client.post(f"/ventures/{vid}/autonomy", json={"level": "L9"}, headers=H).status_code == 422


def test_credit_request_is_recorded_once_a_day(client):
    from sqlalchemy import select
    from app.db.models import CreditAccount, CreditLedgerEntry
    from app.state import db

    vid = _venture(client)
    def credits_snapshot():
        with db()() as session:
            accounts = session.execute(select(CreditAccount.id, CreditAccount.granted_micro,
                CreditAccount.spent_micro, CreditAccount.reserved_micro).where(CreditAccount.venture_id == vid)).all()
            ids = [row.id for row in accounts]
            ledger = session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.kind,
                CreditLedgerEntry.amount_micro).where(CreditLedgerEntry.account_id.in_(ids))).all()
            return accounts, ledger
    before = credits_snapshot()
    assert before[0]  # Exercise a real provisioned balance, not only empty rows.
    assert client.post(f"/ventures/{vid}/credits/request", headers=H).json()["status"] == "requested"
    assert client.post(f"/ventures/{vid}/credits/request", headers=H).json()["status"] == "already_requested"
    items = client.get(f"/ventures/{vid}/feedback").json()["items"]
    assert sum(i["summary"].startswith("Credit request") for i in items) == 1
    assert credits_snapshot() == before  # Saving/deduping a request is not a credit grant.


def test_entitlement_survives_naive_trial_datetimes(client):
    from sqlalchemy import select

    from app.db.models import PlanSubscription
    from app.state import db

    ws = client.post("/workspaces", json={"kind": "venture", "name": "Trial"}).json()
    assert client.get(f"/entitlement?workspace_id={ws['id']}").status_code == 200
    with db()() as session:
        row = session.scalars(select(PlanSubscription).where(PlanSubscription.workspace_id == ws["id"])).one()
        # naive, like Postgres returns for these columns
        row.trial_ends_at = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
        session.commit()
    r = client.get(f"/entitlement?workspace_id={ws['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "free"
