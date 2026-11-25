"""Gateway B1 security contracts that must hold without a live AWS account."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

GW = Path(__file__).resolve().parents[2] / "services" / "gateway"


@pytest.mark.parametrize("env", ["production", "staging", "local", "test"])
@pytest.mark.parametrize("cookie_name", ["cos_session", "legacy_session", "__Host-cos_session"])
def test_session_cookie_fallback_is_local_only(tmp_path, monkeypatch, env, cookie_name):
    main, http, saved = _gateway(tmp_path, monkeypatch)
    try:
        from app.auth import require_session
        from app.config import settings
        from app.session import issue_session
        from fastapi import HTTPException, Request

        monkeypatch.setenv("APP_ENV", env)
        monkeypatch.setattr(settings, "cookie_name", "legacy_session")
        token, _ = issue_session(subject="usr_audit")
        request = Request({"type": "http", "method": "GET", "path": "/auth/session", "headers": [(b"cookie", f"{cookie_name}={token}".encode())]})
        if env in {"production", "staging"} and cookie_name != "__Host-cos_session":
            with pytest.raises(HTTPException) as denied:
                require_session(request)
            assert denied.value.status_code == 401
        else:
            assert require_session(request).subject == "usr_audit"
    finally:
        http.close()
        _close_gateway(saved)


@pytest.mark.parametrize("provider,signature_headers", [
    ("stripe", {"Stripe-Signature": "t=123,v1=audit"}),
    ("razorpay", {"X-Razorpay-Signature": "audit"}),
    ("stub", {"webhook-id": "evt_audit", "webhook-timestamp": "123", "webhook-signature": "audit"}),
    ("stub", {"x-webhook-id": "evt_audit", "x-webhook-timestamp": "123", "x-webhook-signature": "audit"}),
])
def test_payments_webhook_forwards_signature_headers(tmp_path, monkeypatch, provider, signature_headers):
    main, http, saved = _gateway(tmp_path, monkeypatch)
    try:
        raw = b'{ "id": "evt_audit", "amount": 500 }'

        async def forward(path, request, session=None, extra_headers=None):
            assert path == f"webhooks/payments/{provider}"
            assert session is None
            assert await request.body() == raw
            assert extra_headers == {key.lower(): value for key, value in signature_headers.items()}
            return JSONResponse({"ok": True})

        monkeypatch.setattr(main, "forward", forward)
        response = http.post(f"/webhooks/payments/{provider}", content=raw,
                             headers={**signature_headers, "X-Subject": "usr_attacker"})
        assert response.status_code == 200, response.text
    finally:
        http.close()
        _close_gateway(saved)


def _gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("GATEWAY_USERS_DB", str(tmp_path / "users.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_MAIL_SINK", str(tmp_path / "mail.jsonl"))
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "changeme")
    monkeypatch.setenv("SESSION_SECRET", "dev-session-secret-change-me-32b!!")
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "app" or k.startswith("app.")}
    sys.path.insert(0, str(GW))
    main = importlib.import_module("app.main")
    return main, TestClient(main.create_app()), saved


def _close_gateway(saved):
    for key in list(sys.modules):
        if key == "app" or key.startswith("app."):
            sys.modules.pop(key, None)
    sys.modules.update(saved)
    if str(GW) in sys.path:
        sys.path.remove(str(GW))


def test_password_reset_consume_revokes_cookie_and_device_credentials(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        from app import device_auth, users
        from app.session import issue_session, load_session

        user = users.create_user("recovery@example.test", "original-password")
        cookie, _ = issue_session(user.id)
        client.cookies.set("cos_session", cookie)
        device = device_auth.create_device(person_id=user.id, name="Recovery Mac")
        refresh = device_auth.rotate_refresh(device, previous=None)
        access = device_auth.issue_access_jwt(user.id, device)
        reset = users.issue_token(user.id, "password_reset")
        sibling_reset = users.issue_token(user.id, "password_reset")
        magic = users.issue_token(user.id, "magic_link")
        response = client.post("/auth/password-reset/consume", json={"token": reset, "password": "replacement-password"})
        replay = client.post("/auth/password-reset/consume", json={"token": sibling_reset, "password": "another-password"})
        assert replay.status_code == 400
        assert replay.json()["error"]["code"] == "invalid_token"
        assert users.consume_token(magic, "magic_link") is None
        assert response.status_code == 200, response.text
        assert client.get("/auth/session").json()["authenticated"] is False
        assert load_session(cookie) is None
        assert device_auth.load_bearer(access) is None
        with pytest.raises(ValueError, match="revoked"):
            device_auth.rotate_refresh(device, previous=refresh)
        assert users.verify_credentials(user.email, "replacement-password") is not None
    finally:
        _close_gateway(saved)


def test_auth_mail_never_returns_signup_or_recovery_tokens(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        signup = client.post("/auth/signup", json={"email": "founder@example.com", "password": "password1234"})
        assert signup.status_code == 200
        assert "token" not in signup.text
        assert "verify_token" not in signup.text

        unknown = client.post("/auth/magic-link", json={"email": "unknown@example.com"})
        known = client.post("/auth/magic-link", json={"email": "founder@example.com"})
        assert unknown.status_code == known.status_code == 200
        assert unknown.json() == known.json() == {"ok": True}

        reset = client.post("/auth/password-reset", json={"email": "founder@example.com"})
        assert reset.status_code == 200
        assert "token" not in reset.text

        resend_unknown = client.post(
            "/auth/verification/resend", json={"email": "unknown@example.com"}
        )
        resend_known = client.post(
            "/auth/verification/resend", json={"email": "founder@example.com"}
        )
        assert resend_unknown.status_code == resend_known.status_code == 200
        assert resend_unknown.json() == resend_known.json() == {"ok": True}

        rows = [json.loads(line) for line in (tmp_path / "mail.jsonl").read_text().splitlines()]
        assert {row["purpose"] for row in rows} >= {"email_verify", "magic_link", "password_reset"}
        assert all(row["token"] not in signup.text + known.text + reset.text for row in rows)
    finally:
        _close_gateway(saved)


def test_password_recovery_is_generic_and_reports_unavailable_delivery(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        from app import mail, users

        signup = client.post("/auth/signup", json={"email": "founder@example.com", "password": "password1234"})
        assert signup.status_code == 200
        for email in ("founder@example.com", "missing@example.com"):
            result = client.post("/auth/password-reset", json={"email": email})
            assert result.status_code == 200
            assert result.json() == {"ok": True}
        monkeypatch.setattr(mail, "delivery_configured", lambda: False)

        def unexpected_lookup(_email):
            raise AssertionError("No lookup before mail availability")

        monkeypatch.setattr(users, "get_user_by_email", unexpected_lookup)
        for email in ("founder@example.com", "missing@example.com"):
            result = client.post("/auth/password-reset", json={"email": email})
            assert result.status_code == 503
            assert result.json() == {"detail": {"code": "auth_mail_unavailable"}}
    finally:
        _close_gateway(saved)


def test_workspace_invite_is_emailed_with_absolute_one_use_link(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        async def created(*_args, **_kwargs):
            return JSONResponse(
                {
                    "id": "inv_test",
                    "workspace_id": "wsp_test",
                    "email": "friend@example.com",
                    "role": "contributor",
                    "status": "pending",
                    "expires_at": "2026-09-27T00:00:00+00:00",
                    "link": "/invite/one-use-token",
                },
                status_code=201,
            )

        monkeypatch.setattr(main, "forward", created)
        login = client.post("/auth/login", json={"passphrase": "changeme"})
        response = client.post(
            "/api/workspaces/wsp_test/invites",
            json={"email": "friend@example.com", "role": "contributor"},
            headers={"X-CSRF-Token": login.json()["csrf"]},
        )
        assert response.status_code == 201
        assert response.json()["delivery"] == {"status": "sent", "channel": "email"}
        rows = [json.loads(line) for line in (tmp_path / "mail.jsonl").read_text().splitlines()]
        invite = next(row for row in rows if row["purpose"] == "workspace_invite")
        assert invite["to"] == "friend@example.com"
        assert invite["invite_url"] == "https://app.supymem.com/invite/one-use-token"
    finally:
        _close_gateway(saved)


def test_hosted_signup_fails_before_persisting_when_mail_is_unconfigured(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setattr(main.settings, "app_env", "production")
        signup = client.post(
            "/auth/signup", json={"email": "stranded@example.com", "password": "password1234"}
        )
        assert signup.status_code == 503
        from app import users

        assert users.get_user_by_email("stranded@example.com") is None
    finally:
        _close_gateway(saved)


def test_hosted_password_login_requires_verification_with_resend_path(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        signup = client.post(
            "/auth/signup", json={"email": "unverified@example.com", "password": "password1234"}
        )
        assert signup.status_code == 200
        client.post("/auth/logout")
        monkeypatch.setattr(main.settings, "app_env", "production")
        denied = client.post(
            "/auth/login", json={"email": "unverified@example.com", "password": "password1234"}
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "email_verification_required"
        # The account is recoverable without revealing whether a queried address exists.
        monkeypatch.setattr(main.settings, "app_env", "local")
        resent = client.post("/auth/verification/resend", json={"email": "unverified@example.com"})
        assert resent.status_code == 200
    finally:
        _close_gateway(saved)


@pytest.mark.parametrize("token", ["old", "random"])
def test_postgres_refresh_reuse_commits_revocation_before_error(tmp_path, monkeypatch, token):
    main, _client, saved = _gateway(tmp_path, monkeypatch)
    try:
        from app import device_auth, users

        executed: list[str] = []
        events: list[tuple[str, str]] = []

        class _Rows:
            def mappings(self):
                return self

            def first(self):
                return {
                    "refresh_token_hash": "stored",
                    "previous_refresh_token_hash": users._hash_token("old"),
                    "refresh_rotated_at": None,
                    "revoked_at": None,
                }

        class _Conn:
            def execute(self, statement, _params):
                executed.append(str(statement))
                return _Rows()

        class _Tx:
            committed = False

            def __enter__(self):
                return _Conn()

            def __exit__(self, exc_type, _exc, _tb):
                self.committed = exc_type is None
                return False

        tx = _Tx()

        class _Engine:
            def begin(self):
                return tx

        monkeypatch.setattr(device_auth, "_is_postgres", lambda: True)
        monkeypatch.setattr(users, "_pg_engine", lambda: _Engine())
        monkeypatch.setattr(device_auth, "_security_event", lambda kind, device: events.append((kind, device)))
        try:
            device_auth.rotate_refresh("dev_test", previous=token)
            assert False, "invalid refresh must be rejected"
        except ValueError as exc:
            assert str(exc) == ("token_reuse" if token == "old" else "invalid_refresh")
        assert tx.committed is (token == "old")
        assert any(statement.lstrip().startswith("UPDATE") for statement in executed) is (token == "old")
        assert events == ([("token_reuse", "dev_test")] if token == "old" else [])
    finally:
        _close_gateway(saved)


def test_vault_and_events_require_authoritative_venture_access(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        seen = []

        async def denied(*_args, **_kwargs):
            seen.append((_args[0], _args[1].method))
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        monkeypatch.setattr(main, "forward", denied)
        login = client.post("/auth/login", json={"passphrase": "changeme"})
        csrf = login.json()["csrf"]
        vault = client.post(
            "/vault",
            json={"venture_id": "ven_foreign", "key": "k", "value": "secret-token-123456"},
            headers={"X-CSRF-Token": csrf},
        )
        assert vault.status_code == 404
        events = client.get("/events", params={"venture_id": "ven_foreign", "once": "true"})
        assert events.status_code == 404
        missing = client.get("/events", params={"once": "true"})
        assert missing.status_code == 422
        assert seen == [("ventures/ven_foreign", "GET"), ("ventures/ven_foreign", "GET")]
    finally:
        _close_gateway(saved)


def test_device_browser_page_requires_an_explicit_approval(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        login = client.post("/auth/login", json={"passphrase": "changeme"})
        challenge = "a" * 43
        page = client.get(
            "/auth/device/approve",
            params={"code_challenge": challenge, "device_name": "Founder's Mac"},
        )
        assert page.status_code == 200
        assert "Connect Supymem for Mac" in page.text
        assert "fallback_code" not in page.text
        assert "supymem://auth/callback?code=" not in page.text
        assert "window.location.assign(data.redirect)" in page.text
        assert "const localManualCode=true" in page.text
        assert 'id="approval-code"' in page.text
        assert "Paste this code into Supymem" in page.text
        assert '<h1 id="title">You&#8217;re signed in</h1>' in page.text
        assert "You can close this tab once Supymem opens." in page.text
        assert "company workspace" not in page.text
        assert "fallback-instructions" not in page.text
        assert '<details id="fallback" hidden><summary>Having trouble?</summary>' in page.text
        assert '<button id="approve" type="button">Approve this Mac</button>' in page.text
        assert '<a id="open" class="fallback-link" hidden>Open Supymem</a>' in page.text
        assert "button.hidden=true" in page.text
        assert "document.getElementById('fallback').open=true" in page.text
        assert "document.getElementById('open').hidden=false" in page.text
        assert "Open Supymem" in page.text
        assert "X-CSRF-Token" in page.text
        assert login.json()["csrf"]
    finally:
        _close_gateway(saved)


def test_device_browser_page_uses_console_logo_with_a_restricted_image_csp(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(main.settings, "console_origin", "https://app.supymem.com/")
        client.post("/auth/login", json={"passphrase": "changeme"})
        page = client.get("/auth/device/approve", params={"code_challenge": "a" * 43})
        assert page.status_code == 200
        assert '<img class="mark" src="https://app.supymem.com/supymem-logo.svg" alt="">' in page.text
        assert "#93c5fd" not in page.text
        csp = page.headers["content-security-policy"]
        assert "img-src https://app.supymem.com;" in csp
        assert "default-src 'none';" in csp
        assert "base-uri 'none'; frame-ancestors 'none'" in csp
    finally:
        _close_gateway(saved)


def test_ventures_never_fall_back_to_process_local_success(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        async def unavailable(*_args, **_kwargs):
            return JSONResponse({"error": {"code": "upstream_unavailable"}}, status_code=502)

        monkeypatch.setattr(main, "forward", unavailable)
        login = client.post("/auth/login", json={"passphrase": "changeme"})
        csrf = login.json()["csrf"]
        listed = client.get("/api/ventures")
        assert listed.status_code == 502
        created = client.post(
            "/api/ventures",
            json={"raw_idea": "a real founder idea"},
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 502
        assert not hasattr(main, "_VENTURES")
    finally:
        _close_gateway(saved)
