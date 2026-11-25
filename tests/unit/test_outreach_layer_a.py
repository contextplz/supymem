"""OT-48: public Layer A ingest strips emails/LinkedIn URLs; harvest sweep stays 403."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot48.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    return {"main": main, "wid": ensure_desk(LOCAL_PERSON_ID).id}


def test_sweep_still_403(setup):
    with TestClient(setup["main"].create_app()) as client:
        r = client.post("/api/v1/outreach/signal-sweep", json={}, headers={"X-Workspace-Id": setup["wid"]})
        assert r.status_code == 403
        assert r.json()["blocked"] == "layer_a_http_disabled"


def test_shared_row_has_no_email_or_linkedin(setup):
    from app.db.models import GtmContact, GtmPublicDocument
    from app.gtm.relationships.layer_a import ingest_public_documents
    from app.state import db

    with db()() as session:
        before = len(list(session.scalars(select(GtmContact))))

    out = ingest_public_documents(
        None,
        [
            {
                "kind": "hn",
                "title": "Show HN: widgets",
                "url": "https://news.ycombinator.com/item?id=1",
                "payload": {
                    "email": "founder@startup.test",
                    "linkedin_url": "https://www.linkedin.com/in/someone",
                    "snippet": "Hiring. email me at leak@corp.test",
                },
            }
        ],
    )
    doc = out["documents"][0]
    assert doc["email"] is None
    assert doc["linkedin_url"] is None
    assert "email" not in doc["payload"]
    assert "linkedin_url" not in doc["payload"]
    blob = str(doc["payload"])
    assert "founder@startup.test" not in blob
    assert "leak@corp.test" not in blob
    with db()() as session:
        rows = list(session.scalars(select(GtmPublicDocument)))
        assert rows
        payload = rows[0].payload_json
        assert "founder@startup.test" not in payload
        contacts = list(session.scalars(select(GtmContact)))
        assert len(contacts) == before


def test_layer_a_http_does_not_create_named_emails(setup):
    headers = {"X-Workspace-Id": setup["wid"]}
    with TestClient(setup["main"].create_app()) as client:
        r = client.post(
            "/api/v1/outreach/layer-a/ingest",
            json={
                "shared": True,
                "documents": [
                    {
                        "kind": "job",
                        "title": "Staff engineer",
                        "url": "https://example.invalid/jobs/1",
                        "payload": {"contact_email": "a@x.test", "email": "b@x.test"},
                    }
                ],
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        payload = r.json()["documents"][0]["payload"]
        assert "email" not in payload
        assert "contact_email" not in payload
