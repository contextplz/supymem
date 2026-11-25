"""OT-22: HTTP ingest → score → draft → copy → sent; discover still 410."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot22.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    return {"main": main, "wid": desk.id}


def test_http_loop_and_discover_410(setup):
    headers = {"X-Workspace-Id": setup["wid"]}
    with TestClient(setup["main"].create_app()) as client:
        disc = client.post("/api/v1/outreach/discover", json={}, headers=headers)
        assert disc.status_code == 410

        ob = client.patch(
            "/api/v1/outreach/onboarding",
            json={
                "who": "Founders at developer-tool companies who live in Slack",
                "roles": ["founder"],
                "excludes": ["competitor.com"],
                "sender_geo": "US",
                "sender_postal_address": "1 Main St, Oakland, CA 94612",
                "outbound_mode": "human_clipboard",
                "voice": {"write_like": "short and human"},
            },
            headers=headers,
        )
        assert ob.status_code == 200

        ingested = client.post(
            "/api/v1/outreach/ingest",
            json={
                "source": "profile_paste",
                "raw_text": (
                    "Sam Builder\nsam@sample-tools.test\nFounder\n"
                    '"we still run this workflow in a spreadsheet"\n'
                ),
                "trigger_code": "hiring_complaint",
                "enrich": False,
            },
            headers=headers,
        )
        assert ingested.status_code == 200, ingested.text
        cid = ingested.json()["contact_id"]
        assert cid

        patched = client.patch(
            f"/api/v1/outreach/contacts/{cid}",
            json={
                "why_this_week": "They posted a hiring complaint this week",
                "recipient_geo": "US",
                "role": "founder",
            },
            headers=headers,
        )
        assert patched.status_code == 200

        scored = client.post(
            "/api/v1/outreach/score",
            json={"contact_id": cid},
            headers=headers,
        )
        assert scored.status_code == 200
        assert scored.json().get("personalize") == "ok"

        draft = client.post(
            "/api/v1/outreach/drafts",
            json={"contact_id": cid, "kind": "connect"},
            headers=headers,
        )
        assert draft.status_code == 200, draft.text
        did = draft.json()["id"]
        assert draft.json().get("body")

        copied = client.post(f"/api/v1/outreach/drafts/{did}/copy", headers=headers)
        assert copied.status_code == 200
        assert copied.json()["sent_at"] is None

        sent = client.post(
            f"/api/v1/outreach/drafts/{did}/sent",
            json={"channel": "linkedin_connect"},
            headers=headers,
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["sent_at"]
