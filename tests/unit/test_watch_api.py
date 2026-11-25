"""Watch API installs a Huginn graph and accepts a signed webhook."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/watch.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


def test_watch_graph_and_webhook(client: TestClient):
    created = client.post(
        "/ventures",
        json={"raw_idea": "invoice chaser for freelancers who hate chasing"},
    )
    assert created.status_code == 201, created.text
    vid = created.json()["id"]

    view = client.get(f"/ventures/{vid}/watch")
    assert view.status_code == 200, view.text
    body = view.json()
    types = {a["type"] for a in body["agents"]}
    assert "DigestAgent" in types
    assert "WebhookAgent" in types
    assert "PeakDetectorAgent" in types

    from app.watch.bridge import webhook_secret

    secret = webhook_secret(vid)
    assert secret
    hooked = client.post(
        f"/ventures/{vid}/watch/webhooks/{secret}",
        json={"title": "someone signed the waitlist", "message": "new signup"},
    )
    assert hooked.status_code == 200, hooked.text

    ran = client.post(f"/ventures/{vid}/watch/run", json={"force": True})
    assert ran.status_code == 200, ran.text
    assert ran.json()["agents"] >= 3
