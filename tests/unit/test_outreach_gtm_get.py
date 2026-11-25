"""OT-12: gtm_get is the only contact load for outreach; foreign workspace → None."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def two_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot12.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.gtm.supymem.pipeline import ingest_leads
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    desk_a = ensure_desk(LOCAL_PERSON_ID)
    ws_b = create_workspace(
        kind="company",
        name="TenantB",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
    )
    ingested = ingest_leads(
        desk_a.id,
        [
            {
                "first_name": "Ada",
                "email": "ada@sample-analytics.test",
                "email_confidence": "safe",
                "organization": "Analytical",
                "source": "test",
            }
        ],
        enrich=False,
    )
    return desk_a.id, ws_b.id, ingested["contacts"][0]["id"]


def test_foreign_workspace_returns_none(two_ws):
    from app.db.models import GtmContact
    from app.gtm.relationships.scope import gtm_get
    from app.state import db

    wid_a, wid_b, cid = two_ws
    with db()() as session:
        assert gtm_get(session, GtmContact, cid, wid_a) is not None
        assert gtm_get(session, GtmContact, cid, wid_b) is None


def test_bare_pk_forbidden_in_listed_modules():
    root = Path(__file__).resolve().parents[2]
    listed = [
        root / "services/company_os/app/gtm/relationships/outreach.py",
        root / "services/company_os/app/gtm/relationships/dossier.py",
        root / "services/company_os/app/actuation/tools/email_sequence.py",
        root / "services/company_os/app/api/ops_api.py",
    ]
    needle = "session.get(GtmContact"
    for path in listed:
        text = path.read_text()
        if path.name == "ops_api.py":
            # Only the outreach section must not load by bare PK.
            start = text.find("GTM outreach")
            end = text.find("AI GTM — OpenLeads", start)
            chunk = text[start:end] if start != -1 else text
            assert needle not in chunk, path
        else:
            assert needle not in text, path
