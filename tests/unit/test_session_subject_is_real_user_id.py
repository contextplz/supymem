from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

GW = Path(__file__).resolve().parents[2] / "services" / "gateway"


def test_issue_session_accepts_user_id(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "dev-session-secret-change-me-32b!!")
    # Isolate gateway app.* from company_os app.* already on sys.path.
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "app" or k.startswith("app.")}
    sys.path.insert(0, str(GW))
    try:
        session = importlib.import_module("app.session")
        token, sess = session.issue_session("usr_abc123")
        assert sess.subject == "usr_abc123"
        loaded = session.load_session(token)
        assert loaded is not None
        assert loaded.subject == "usr_abc123"
    finally:
        for k in list(sys.modules):
            if k == "app" or k.startswith("app."):
                sys.modules.pop(k, None)
        sys.modules.update(saved)
        if str(GW) in sys.path:
            sys.path.remove(str(GW))


@pytest.mark.parametrize("domain", ["vanta.com", "ouraring.com"])
def test_session_lookup_keeps_original_expiry(monkeypatch, domain):
    saved = {name: sys.modules.pop(name) for name in list(sys.modules) if name == "app" or name.startswith("app.")}
    monkeypatch.syspath_prepend(str(GW))
    try:
        session = importlib.import_module("app.session")
        monkeypatch.setattr(session, "get_user", lambda subject: None)
        monkeypatch.setattr(session.settings, "session_ttl_hours", 1)
        issued_at = 1_800_000_000.0
        monkeypatch.setattr(session.time, "time", lambda: issued_at)
        token, original = session.issue_session("usr_expiry", email=f"you@{domain}")
        monkeypatch.setattr(session.time, "time", lambda: issued_at + 3599)
        loaded = session.load_session(token)
        assert loaded is not None
        assert loaded.expires_at == original.expires_at
        assert loaded.email == original.email
        assert loaded.authenticated
        monkeypatch.setattr(session.time, "time", lambda: original.expires_at)
        assert not loaded.authenticated
        expired = session.load_session(token)
        assert expired is None or not expired.authenticated
        monkeypatch.setattr(session.time, "time", lambda: original.expires_at + 1)
        assert session.load_session(token) is None
    finally:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)
        sys.modules.update(saved)
