"""Actual signed discovery API and tenant persistence; provider search is stubbed."""
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_core_signed_boundary import headers, make_workspace


def test_find_persists_unverified_evidence_without_paid_or_background_work(client, monkeypatch):
    from app.gtm import lead_pipeline
    from app.gtm.supymem import pipeline
    from app.gtm.localization import experiments
    from app.gtm.relationships.icp import email_confidence_allowed
    from app.state import db
    from app.db.models import GtmContact
    from sqlalchemy import select
    wid = make_workspace(client, "usr_alice")
    monkeypatch.setattr(experiments, "execution_context", lambda wid: {"allowed": True})
    def forbidden(*a, **kw):
        raise AssertionError("discovery must not invoke enrichment, queue research or send")
    monkeypatch.setattr(pipeline.hunter, "configured", forbidden)
    monkeypatch.setattr(pipeline, "queue_research_job", forbidden)
    monkeypatch.setattr(pipeline, "start_outreach", forbidden)
    lead = {"email": "care@clinic.example", "website": "https://clinic.example/", "organization": "clinic.example",
            "source": "public_web", "source_url": "https://clinic.example/contact", "email_confidence": "unverified",
            "evidence": {"kind": "published_company_contact", "url": "https://clinic.example/contact", "observed_at": "2026-09-10T00:00:00+00:00"},
            "target_match": {"basis": "company_page_text", "terms": ["dentists"], "requested_region": "Pune"}}
    calls = []
    def find(**kw):
        calls.append(kw)
        return {"leads": [lead.copy()], "count": 1, "safe": 0, "unverified": 1, "state": "completed"}
    monkeypatch.setattr(lead_pipeline, "find_leads_combined", find)
    path = "/api/v1/ai-gtm/find"
    response = client.post(path, params={"workspace_id": wid}, headers=headers(path, "POST", "usr_alice"),
                           json={"query": "dentists", "industry": "dental", "region": "Pune", "count": 1, "auto_outreach": False})
    assert response.status_code == 200, response.text
    assert calls[0]["query"] == "dentists" and calls[0]["region"] == "Pune"
    contact = response.json()["contacts"][0]
    assert contact["email_verification"] == "unverified" and contact["source_url"].endswith("/contact")
    assert contact["evidence"]["kind"] == "published_company_contact"
    assert contact["target_match"]["requested_region"] == "Pune"
    with db()() as session:
        row = session.scalar(select(GtmContact).where(GtmContact.workspace_id == wid))
        assert row.email_confidence == "guess" and not email_confidence_allowed(row.email_confidence)
        assert row.lawful_basis == "none"
    denied = client.post(path, params={"workspace_id": wid}, headers=headers(path, "POST", "usr_bob"),
                         json={"query": "dentists", "count": 1})
    assert denied.status_code == 404 and len(calls) == 1


def test_lab_empty_result_keeps_target_and_does_not_broaden_or_enrich(client, monkeypatch):
    from app.gtm import lead_pipeline, lead_scenarios
    from app.gtm.supymem import pipeline
    from app.gtm.localization import experiments
    wid = make_workspace(client, "usr_alice")
    existing = pipeline.ingest_leads(wid, [{"email": "unrelated@studio.example", "source": "public_web",
                                          "website": "https://studio.example", "organization": "Unrelated studio"}], enrich=False)
    assert len(existing["contacts"]) == 1
    monkeypatch.setattr(experiments, "execution_context", lambda wid: {"allowed": True})
    monkeypatch.setattr(lead_scenarios, "resolve_scenario", lambda **kw: {"id": "specific", "mode": "preset_random", "query": "dentists in Pune", "geo": "Pune", "industry": "dental"})
    monkeypatch.setattr(pipeline.hunter, "configured", lambda: (_ for _ in ()).throw(AssertionError("no paid enrichment")))
    calls = []
    def find(**kw):
        calls.append(kw)
        return {"leads": [], "count": 0, "state": "no_matches", "shortfall": 3, "message": "No matching company evidence."}
    monkeypatch.setattr(lead_pipeline, "find_leads_combined", find)
    path = "/api/v1/ai-gtm/lab/generate"
    result = client.post(path, params={"workspace_id": wid}, headers=headers(path, "POST", "usr_alice"),
                         json={"query": "dentists in Pune", "count": 3, "random": True, "enable_discovery": False})
    assert result.status_code == 200, result.text
    assert len(calls) == 1 and calls[0]["query"] == "dentists in Pune"
    assert result.json()["state"] == "no_matches" and result.json()["shortfall"] == 3
    assert result.json()["contacts"] == []
