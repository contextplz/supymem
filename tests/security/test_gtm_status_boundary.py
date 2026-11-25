"""The actual signed GTM status route uses the shared Foundry provider contract."""
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_core_signed_boundary import headers, make_workspace


def test_gtm_status_returns_scoped_engine_and_current_llm_contract(client, monkeypatch):
    from app.gtm import lead_pipeline
    # Only external executable discovery is stubbed; status assembly and scoped
    # engine/funnel/localization reads are the production implementations.
    monkeypatch.setattr(lead_pipeline, "status", lambda: {"ready": False})
    wid = make_workspace(client, "usr_alice")
    path = "/api/v1/ai-gtm/status"
    result = client.get(path, params={"workspace_id": wid}, headers=headers(path, "GET", "usr_alice"))
    assert result.status_code == 200, result.text
    body = result.json()
    assert "llm" in body and "anthropic" not in body
    assert body["engine"]["workspace_id"] == wid
    assert body["engine"]["send_enabled"] is False and body["dry_run"] is True
    assert not any(word in body["llm"] for word in ("key", "api_key", "secret"))
    denied = client.get(path, params={"workspace_id": wid}, headers=headers(path, "GET", "usr_bob"))
    assert denied.status_code == 404


def test_content_template_has_no_canned_business_claims_and_requires_founder_input(client):
    from app.content.brief import BriefError, validate
    import pytest
    wid = make_workspace(client, "usr_alice")
    path = "/api/v1/content/brief/template"
    response = client.get(path, params={"workspace_id": wid}, headers=headers(path, "GET", "usr_alice"))
    assert response.status_code == 200, response.text
    template = response.json()
    assert template["claims"] == [] and template["competitors"] == []
    assert not template["product"]["url"] and not template["product"]["name"]
    with pytest.raises(BriefError):
        validate(template)
    from tests.content_fixtures import filled_brief
    from app.content.brief import save
    from datetime import datetime
    saved = save(workspace_id=wid, data=filled_brief(), source_name="acceptance")
    assert datetime.fromisoformat(saved["created_at"]).utcoffset().total_seconds() == 0
