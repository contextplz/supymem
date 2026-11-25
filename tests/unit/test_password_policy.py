from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

GW = Path(__file__).resolve().parents[2] / "services" / "gateway"


@pytest.fixture
def users_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("GATEWAY_USERS_DB", str(tmp_path / "users.sqlite3"))
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "app" or k.startswith("app.")}
    sys.path.insert(0, str(GW))
    users = importlib.import_module("app.users")
    try:
        yield users
    finally:
        for key in list(sys.modules):
            if key == "app" or key.startswith("app."):
                sys.modules.pop(key, None)
        sys.modules.update(saved)
        if str(GW) in sys.path:
            sys.path.remove(str(GW))


def test_create_user_distinguishes_password_policy_from_email_taken(users_mod, monkeypatch):
    with pytest.raises(ValueError, match="^password_too_short$"):
        users_mod.create_user("founder@example.com", "short")
    monkeypatch.setattr(users_mod, "hibp_breached", lambda _password: True)
    with pytest.raises(ValueError, match="^password_breached$"):
        users_mod.create_user("founder@example.com", "long-enough-password")
    monkeypatch.setattr(users_mod, "hibp_breached", lambda _password: False)
    created = users_mod.create_user("founder@example.com", "long-enough-password")
    assert created.email == "founder@example.com"
    with pytest.raises(ValueError, match="^email_taken$"):
        users_mod.create_user("founder@example.com", "another-long-password")


def test_set_password_uses_the_same_policy_codes(users_mod, monkeypatch):
    user = users_mod.create_user("reset@example.com", "initial-password")
    with pytest.raises(ValueError, match="^password_too_short$"):
        users_mod.set_password(user.id, "tiny")
    monkeypatch.setattr(users_mod, "hibp_breached", lambda _password: True)
    with pytest.raises(ValueError, match="^password_breached$"):
        users_mod.set_password(user.id, "replacement-password")


def test_hibp_range_query_never_sends_the_password(users_mod, monkeypatch):
    seen: dict[str, str] = {}

    class _Resp:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(req, timeout=2):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(users_mod, "urlopen", fake_urlopen)
    secret = "unique-founder-password"
    assert users_mod.hibp_breached(secret) is False
    assert secret not in seen["url"]
    assert seen["url"].startswith("https://api.pwnedpasswords.com/range/")
    assert len(seen["url"].rsplit("/", 1)[-1]) == 5
