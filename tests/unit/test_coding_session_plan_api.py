"""POST /coding/sessions/plan — consent, PATH, cloud forbid, no spawn."""

from __future__ import annotations

import pytest
from coding_agents.detect import DetectedAgent
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


def _fake_detect(_path=None):
    return [
        DetectedAgent(
            name="claude",
            binary="/opt/homebrew/bin/claude",
            version="fake",
            auth_mode="subscription",
        )
    ]


def _boot(tmp_path, monkeypatch, *, env: str = "local") -> TestClient:
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/session.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CODING_AGENT_CONSENT_DB", str(tmp_path / "consent.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COMPANY_OS_ENV", env)
    main = load_company_os_app()
    from app import state as state_mod
    import app.api.coding_api as coding_api

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    monkeypatch.setattr(coding_api, "detect", _fake_detect)
    return TestClient(main.create_app())


@pytest.mark.parametrize("domain", ["warehouse-dispatch", "weekend-gardening"])
def test_plan_requires_consent(tmp_path, monkeypatch, domain):
    from coding_agents.consent import has_consent, record_consent

    monkeypatch.setenv("CODING_AGENT_CONSENT_DB", str(tmp_path / domain / "previous.sqlite3"))
    record_consent("claude", "previous-version")
    assert has_consent("claude", "previous-version")
    with _boot(tmp_path, monkeypatch) as client:
        r = client.post("/coding/sessions/plan", json={"provider": "claude"})
        assert r.status_code == 403
        assert r.json()["detail"]["error"]["code"] == "consent_required"


def test_plan_returns_interactive_argv_after_consent(tmp_path, monkeypatch):
    with _boot(tmp_path, monkeypatch) as client:
        client.headers.update({"X-Subject": "usr_desktop_owner"})
        created = client.post("/ventures", json={
            "raw_idea": "A waitlist for neighborhood bakers selling custom cakes", "start": False,
        })
        assert created.status_code == 201, created.text
        venture_id = created.json()["id"]
        c = client.post("/coding/consent", json={"adapter": "claude", "version": "fake"})
        assert c.status_code == 200
        r = client.post(
            "/coding/sessions/plan",
            json={"provider": "claude", "venture_id": venture_id, "auto_approve": False},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["argv"][0] == "claude"
        assert "--ide" in body["argv"]
        assert "-p" not in body["argv"]
        assert "bypassPermissions" not in body["argv"]
        assert body["binary"].endswith("claude")
        assert body["cwd"].endswith(f"ventures/{venture_id}/repo")
        assert "ANTHROPIC_API_KEY" in body["scrub_vars"]
        assert "your subscription" in body["budget"].lower()
        # Consent does not confer access to another founder's workspace.
        client.headers.update({"X-Subject": "usr_other_founder"})
        denied = client.post("/coding/sessions/plan", json={"provider": "claude", "venture_id": venture_id})
        assert denied.status_code == 404


def test_plan_forbidden_on_cloud(tmp_path, monkeypatch):
    with _boot(tmp_path, monkeypatch, env="cloud") as client:
        client.post("/coding/consent", json={"adapter": "claude", "version": "fake"})
        r = client.post("/coding/sessions/plan", json={"provider": "claude"})
        assert r.status_code == 403
        assert r.json()["detail"]["error"]["code"] == "cloud_forbids_byo"


def test_plan_unknown_provider(tmp_path, monkeypatch):
    with _boot(tmp_path, monkeypatch) as client:
        r = client.post("/coding/sessions/plan", json={"provider": "munder"})
        assert r.status_code == 400


def test_plan_missing_cli_says_not_on_path(tmp_path, monkeypatch):
    with _boot(tmp_path, monkeypatch) as client:
        r = client.post("/coding/sessions/plan", json={"provider": "codex"})
        assert r.status_code == 404
        body = r.json()["detail"]["error"]
        assert body["code"] == "not_on_path"
        assert "codex" in body["message"].lower()
        assert "PATH" in body["message"]
