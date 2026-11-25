from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

GW = Path(__file__).resolve().parents[2] / "services" / "gateway"


def test_signup_login_reset_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_USERS_DB", str(tmp_path / "users.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_MAIL_SINK", str(tmp_path / "mail.jsonl"))
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "changeme")
    monkeypatch.setenv("SESSION_SECRET", "dev-session-secret-change-me-32b!!")

    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "app" or k.startswith("app.")}
    sys.path.insert(0, str(GW))
    try:
        main = importlib.import_module("app.main")
        client = TestClient(main.create_app())

        r = client.post("/auth/signup", json={"email": "a@example.com", "password": "password1234"})
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "account"
        assert r.json()["subject"].startswith("usr_")
        assert "verify_token" not in r.json()

        client.post("/auth/logout")
        r = client.post(
            "/auth/login",
            json={"email": "a@example.com", "password": "password1234"},
        )
        assert r.status_code == 200
        assert r.json()["authenticated"] is True

        r = client.post("/auth/password-reset", json={"email": "a@example.com"})
        assert r.status_code == 200
        assert "token" not in r.json()
        import json as json_mod

        lines = (tmp_path / "mail.jsonl").read_text().strip().splitlines()
        token = json_mod.loads(lines[-1])["token"]
        r = client.post(
            "/auth/password-reset/consume",
            json={"token": token, "password": "newpassword99"},
        )
        assert r.status_code == 200

        client.post("/auth/logout")
        r = client.post(
            "/auth/login",
            json={"email": "a@example.com", "password": "newpassword99"},
        )
        assert r.status_code == 200

        client.post("/auth/logout")
        r = client.post("/auth/login", json={"passphrase": "changeme"})
        assert r.status_code == 200
        assert r.json()["mode"] == "local_passphrase"
    finally:
        for k in list(sys.modules):
            if k == "app" or k.startswith("app."):
                sys.modules.pop(k, None)
        sys.modules.update(saved)
        if str(GW) in sys.path:
            sys.path.remove(str(GW))
