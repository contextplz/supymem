"""Unlimited venture creation keeps authority and one founder allowance."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

IDEA = "a marketplace for restoring vintage synthesizers"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/limit.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ADMIN_BOOTSTRAP_EMAILS", raising=False)
    monkeypatch.delenv("FOUNDER_ADMIN_EMAILS", raising=False)
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    state.MEM.clear()
    with TestClient(main.create_app()) as c:
        from tests.unit.credits.test_wallet_bridge import phase_marker
        phase_marker("legacy")
        yield c


def _create(client, subject="usr_limit"):
    return client.post("/ventures", json={"raw_idea": IDEA, "start": False}, headers={"X-Subject": subject})


def _ledger_rows():
    from sqlalchemy import func, select

    from app.db.models import CreditLedgerEntry
    from app.state import db

    with db()() as s:
        return s.scalar(select(func.count(CreditLedgerEntry.id)))


def test_third_venture_allowed_after_archive_without_another_grant(client):
    ids = []
    for _ in range(2):
        r = _create(client)
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    # Archived ventures do not impose a creation limit.
    from datetime import datetime

    from app.db.models import Venture
    from app.state import db

    with db()() as s:
        s.get(Venture, ids[0]).archived_at = datetime.utcnow()
        s.commit()
    before = _ledger_rows()
    r = _create(client)
    assert r.status_code == 201, r.text
    assert _ledger_rows() == before
    # Another account is unaffected.
    assert _create(client, "usr_other").status_code == 201


def test_chat_entry_preview_is_not_count_limited(client):
    for _ in range(2):
        assert _create(client).status_code == 201
    r = client.post(
        "/entry/chat/preview",
        json={"text": "I want to build a personal app for scheduling rehearsal sessions at my local studio"},
        headers={"X-Subject": "usr_limit"},
    )
    assert r.status_code == 200, r.text


def test_chat_confirm_remains_available_after_two_other_ventures(client):
    r = client.post(
        "/entry/chat/preview",
        json={"text": "I want to build a personal app for scheduling rehearsal sessions at my local studio"},
        headers={"X-Subject": "usr_limit"},
    )
    assert r.status_code == 200, r.text
    pid = r.json()["proposal"]["id"]
    for _ in range(2):
        assert _create(client).status_code == 201
    before = _ledger_rows()
    c = client.post("/entry/chat/confirm", json={"proposal_id": pid}, headers={"X-Subject": "usr_limit"})
    assert c.status_code == 200, c.text
    assert _ledger_rows() == before


def test_creation_needs_no_admin_or_email_exemption(client, monkeypatch):
    from app.db.models import Person
    from app.state import db

    vid = None
    for _ in range(2):
        r = _create(client)
        assert r.status_code == 201
        vid = r.json()["id"]
    from app.db.models import Venture, Workspace

    with db()() as s:
        owner = s.get(Workspace, s.get(Venture, vid).workspace_id).owner_id
        s.get(Person, owner).email = "boss@example.com"
        s.commit()
    assert _create(client).status_code == 201
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "boss@example.com")
    assert _create(client).status_code == 201
