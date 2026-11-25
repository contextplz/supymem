"""Vault secrets never appear in gateway responses or SSE frames."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "services" / "gateway"

SECRET = "sk_live_SUPERSECRETVALUE99xyz"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "test-pass")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    for k in list(sys.modules):
        if k == "app" or k.startswith("app."):
            del sys.modules[k]
    sys.path = [p for p in sys.path if "services/company_os" not in p and "services/warden" not in p]
    sys.path.insert(0, str(GATEWAY))

    from app.config import settings
    import app.auth as auth_mod
    from argon2 import PasswordHasher
    from app.vault import Vault
    import app.vault as vault_mod
    from app.sse_hub import SSEHub
    import app.sse_hub as hub_mod
    import app.main as main_mod
    import app.proxy as proxy_mod

    settings.console_passphrase = "test-pass"
    auth_mod._PASS_HASH = PasswordHasher().hash("test-pass")
    auth_mod.settings.console_passphrase = "test-pass"
    vault_mod.vault = Vault(path=tmp_path / "vault.enc", secret="test-session-secret-at-least-32-bytes")
    hub_mod.sse_hub = SSEHub()
    monkeypatch.setattr(hub_mod, "vault", vault_mod.vault)
    main_mod.sse_hub = hub_mod.sse_hub
    main_mod.vault = vault_mod.vault
    monkeypatch.setattr(proxy_mod, "vault", vault_mod.vault)

    # This suite validates value redaction only. Membership is exercised in the
    # gateway B1 security suite against an authoritative core response.
    async def _authorized(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main_mod, "_require_venture_access", _authorized)

    app = main_mod.create_app()
    with TestClient(app) as c:
        yield c, vault_mod.vault, hub_mod.sse_hub


def test_vault_put_never_echoes_secret(client) -> None:
    c, v, hub = client
    r = c.post(
        "/auth/login",
        json={"passphrase": "test-pass"},
        headers={"Origin": "http://127.0.0.1:3100"},
    )
    csrf = r.json()["csrf"]
    r2 = c.post(
        "/vault",
        json={"venture_id": "ven_1", "key": "stripe", "value": SECRET},
        headers={"Origin": "http://127.0.0.1:3100", "X-CSRF-Token": csrf},
    )
    assert r2.status_code == 200
    assert SECRET not in r2.text
    assert "sk_live" not in r2.text
    listed = c.get("/vault/ven_1", headers={"Origin": "http://127.0.0.1:3100"})
    assert SECRET not in listed.text
    assert listed.json()["items"][0]["key"] == "stripe"


def test_sse_redacts_secret_payload(client) -> None:
    c, v, hub = client
    c.post(
        "/auth/login",
        json={"passphrase": "test-pass"},
        headers={"Origin": "http://127.0.0.1:3100"},
    )
    v.put("ven_1", "stripe", SECRET)
    hub.publish("feedback.received", {"note": f"key={SECRET}"}, venture_id="ven_1")
    with c.stream(
        "GET",
        "/events",
        params={"topics": "feedback.received", "venture_id": "ven_1", "once": "true"},
        headers={"Origin": "http://127.0.0.1:3100"},
    ) as resp:
        buf = "".join(resp.iter_text())
    assert SECRET not in buf
    assert "[REDACTED]" in buf or "sk_live" not in buf


@pytest.mark.parametrize("value", ["<", "/", "short-token"])
def test_vault_rejects_short_values_at_write_time(client, value):
    c, vault, _hub = client
    login = c.post("/auth/login", json={"passphrase": "test-pass"})
    response = c.post("/vault", json={"venture_id": "ven_a", "key": "token", "value": value},
                      headers={"X-CSRF-Token": login.json()["csrf"]})
    assert response.status_code == 422
    assert "at least 12 characters" in response.text
    assert vault.list_handles("ven_a") == []
    with pytest.raises(ValueError, match="at least 12 characters"):
        vault.put("ven_a", "token", value)


@pytest.mark.parametrize("content_type", ["application/json", "text/html"])
def test_proxy_redaction_is_authoritatively_tenant_scoped(client, monkeypatch, content_type):
    c, vault, _hub = client
    from app import proxy

    own_secret = "fixture-value-2026"
    vault.put("ven_a", "token", own_secret, workspace_id="wsp_a")
    for value in ("<", "/", "Content-Type", "</html></body>"):
        vault._data["entries"][f"legacy-{value}"] = {"venture_id": "ven_a", "value": value}
    body = f"<html>https://example.test/{own_secret}</html>"
    real_client = httpx.AsyncClient

    def backend(request):
        scope = request.url.path.split("/")[2] if request.url.path.startswith("/ventures/") else None
        headers = {"X-COS-Venture-ID": scope} if scope else {}
        if request.url.path.startswith("/workspaces/"):
            headers["X-COS-Workspace-ID"] = request.url.path.split("/")[2]
        if content_type == "application/json":
            return httpx.Response(200, json={"text": body}, headers=headers)
        return httpx.Response(200, text=body, headers={**headers, "Content-Type": content_type})

    def make_client(*args, **kwargs):
        return real_client(*args, **kwargs, transport=httpx.MockTransport(backend))

    monkeypatch.setattr(proxy.httpx, "AsyncClient", make_client)
    c.post("/auth/login", json={"passphrase": "test-pass"})
    spoof = {"X-COS-Venture-ID": "ven_a", "X-Workspace-ID": "ven_a"}
    for path in ("/api/ventures/ven_b", "/api/workspaces/wsp_b", "/api/me?venture_id=ven_a", "/w/ven_b/site/"):
        response = c.get(path, headers=spoof)
        assert response.status_code == 200
        assert body in response.text
    for path in ("/api/ventures/ven_a", "/api/workspaces/wsp_a"):
        own = c.get(path)
        assert own_secret not in own.text
        assert "[REDACTED]" in own.text
        assert "<html>" in own.text


@pytest.mark.parametrize("value", ["ordinary response text", "</html></body>", "Content-Type"])
def test_vault_rejects_non_secret_values(client, value):
    c, vault, _hub = client
    login = c.post("/auth/login", json={"passphrase": "test-pass"})
    response = c.post("/vault", json={"venture_id": "ven_a", "key": "token", "value": value},
                      headers={"X-CSRF-Token": login.json()["csrf"]})
    assert response.status_code == 422
    assert "not common or structural text" in response.text
    assert vault.list_handles("ven_a") == []


def test_sse_exact_redaction_is_tenant_scoped(client):
    _c, vault, hub = client
    vault_value = "fixture-value-2026"
    vault.put("ven_a", "token", vault_value)
    assert hub.publish("test", {"text": vault_value}, venture_id="ven_a")["payload"]["text"] == "[REDACTED]"
    for scope in ("ven_b", None):
        assert hub.publish("test", {"text": vault_value}, venture_id=scope)["payload"]["text"] == vault_value
