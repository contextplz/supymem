"""SSE resume via Last-Event-ID is gap-free."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "services" / "gateway"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "test-pass")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    for k in list(sys.modules):
        if k == "app" or k.startswith("app."):
            del sys.modules[k]
    sys.path = [
        p
        for p in sys.path
        if "services/company_os" not in p and "services/warden" not in p
    ]
    sys.path.insert(0, str(GATEWAY))

    from app.config import settings
    import app.auth as auth_mod
    from argon2 import PasswordHasher
    from app.sse_hub import SSEHub
    import app.sse_hub as hub_mod
    import app.main as main_mod

    settings.console_passphrase = "test-pass"
    auth_mod._PASS_HASH = PasswordHasher().hash("test-pass")
    auth_mod.settings.console_passphrase = "test-pass"
    hub = SSEHub()
    hub_mod.sse_hub = hub
    main_mod.sse_hub = hub
    app = main_mod.create_app()
    with TestClient(app) as c:
        yield c, hub


def test_sse_resume_gap_free(client) -> None:
    c, hub = client
    r = c.post(
        "/auth/login",
        json={"passphrase": "test-pass"},
        headers={"Origin": "http://127.0.0.1:3100"},
    )
    assert r.status_code == 200
    e1 = hub.publish("venture.created", {"n": 1}, venture_id="ven_a")
    hub.publish("venture.created", {"n": 2}, venture_id="ven_a")
    hub.publish("venture.created", {"n": 3}, venture_id="ven_a")

    resp = c.get(
        "/events",
        params={"topics": "venture.created", "venture_id": "ven_a", "once": "true"},
        headers={
            "Origin": "http://127.0.0.1:3100",
            "Last-Event-ID": str(e1["seq"]),
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert "id: 2" in body
    assert "id: 3" in body
    assert "id: 1\n" not in body
    seqs = [int(line.split(":", 1)[1].strip()) for line in body.splitlines() if line.startswith("id:")]
    assert seqs == [2, 3]
