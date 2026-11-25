"""Host-only session cookie flags (doc 06 §1.2). Local uses cos_session."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi import Response
from fastapi.testclient import TestClient

GW = Path(__file__).resolve().parents[2] / "services" / "gateway"


def test_local_cookie_is_host_scoped_httponly(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "changeme")
    monkeypatch.setenv("SESSION_SECRET", "dev-session-secret-change-me-32b!!")
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "app" or k.startswith("app.")}
    sys.path.insert(0, str(GW))
    try:
        main = importlib.import_module("app.main")
        client = TestClient(main.create_app())
        r = client.post("/auth/login", json={"passphrase": "changeme"})
        assert r.status_code == 200
        cookie = r.cookies.get("cos_session")
        assert cookie
        header = r.headers.get("set-cookie") or ""
        assert "HttpOnly" in header or "httponly" in header.lower()
        assert "Domain=" not in header
        assert "cos_session=" in header
        assert "__Host-cos_session" not in header
    finally:
        for k in list(sys.modules):
            if k == "app" or k.startswith("app."):
                sys.modules.pop(k, None)
        sys.modules.update(saved)
        if str(GW) in sys.path:
            sys.path.remove(str(GW))


def test_hosted_cookie_name_is_host_prefix(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "app" or k.startswith("app.")}
    sys.path.insert(0, str(GW))
    try:
        from app.auth import session_cookie_name

        assert session_cookie_name() == "__Host-cos_session"
    finally:
        for k in list(sys.modules):
            if k == "app" or k.startswith("app."):
                sys.modules.pop(k, None)
        sys.modules.update(saved)
        if str(GW) in sys.path:
            sys.path.remove(str(GW))
        monkeypatch.setenv("APP_ENV", "local")


def test_hosted_cookie_deletion_keeps_host_prefix_requirements(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "app" or k.startswith("app.")}
    sys.path.insert(0, str(GW))
    try:
        from app.auth import clear_session_cookie

        response = Response()
        clear_session_cookie(response)
        headers = [
            value.decode("latin-1")
            for key, value in response.raw_headers
            if key.lower() == b"set-cookie"
        ]
        host_cookie = next(header for header in headers if header.startswith("__Host-cos_session="))
        assert "Max-Age=0" in host_cookie
        assert "Path=/" in host_cookie
        assert "Secure" in host_cookie
        assert "Domain=" not in host_cookie
    finally:
        for k in list(sys.modules):
            if k == "app" or k.startswith("app."):
                sys.modules.pop(k, None)
        sys.modules.update(saved)
        if str(GW) in sys.path:
            sys.path.remove(str(GW))
        monkeypatch.setenv("APP_ENV", "local")
