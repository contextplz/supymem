"""HTTP loop for GTM God. Discover/harvest must not appear."""

from __future__ import annotations

from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


def test_god_http_start_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/godapi.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path / "firstlook"))
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    headers = {"X-Workspace-Id": desk.id}
    with TestClient(main.create_app()) as client:
        start = client.post(
            "/api/v1/gtm/god/start",
            headers=headers,
            json={"idea": "A first look for teams drowning in Slack search"},
        )
        assert start.status_code == 409, start.text
        assert start.json()["detail"]["code"] == "gtm_quote_required"
        tick = client.post("/api/v1/gtm/god/tick", headers=headers)
        assert tick.status_code == 409, tick.text
        assert tick.json()["detail"]["code"] == "gtm_quote_required"
        status = client.get("/api/v1/gtm/god", headers=headers)
        assert status.status_code == 200
        assert status.json()["plan"] is None
