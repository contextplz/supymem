"""Actual assembled core callbacks, with only provider network boundaries mocked."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import delete, select

from tests.company_os_harness import company_os_test_client, load_company_os_app
from tests.security.test_core_signed_boundary import SECRET, headers, make_workspace


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    for key, value in {
        "APP_ENV": "local", "COMPANY_OS_DB_URL": f"sqlite:///{tmp_path}/core.db",
        "COMPANY_OS_DATA_DIR": str(tmp_path), "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
        # Process-local vault: avoids KeyError when a prior FileSecretStore singleton
        # was bound to another tmp_path / data dir in the same pytest process.
        "COMPANY_OS_EPHEMERAL_VAULT": "1",
        "LLM_OFFLINE": "1", "WILL_SCHEDULER": "0", "CONTENT_ENGINE_ENABLED": "0",
        "CONTENT_AGENT_ENABLED": "0", "MARKETING_EXECUTION_ENABLED": "0", "MARKETING_OUTBOUND_ENABLED": "0",
        "CHANNELS_DRY_RUN": "true", "COMPANY_OS_REQUIRE_SIGNED_CALLER": "1",
        "COS_INTERNAL_ASSERTION_SECRET": SECRET, "COMPANY_OS_VAULT_SECRET": "test-vault-secret-with-more-than-32-bytes",
        "CONTENT_OAUTH_BASE_URL": "https://api.example.test", "CONSOLE_URL": "https://console.example.test",
        "X_CLIENT_ID": "test-client", "X_CLIENT_SECRET": "test-client-secret",
        "ADMIN_REQUIRE_TOTP": "1", "ADMIN_BOOTSTRAP_EMAILS": "ops@example.test",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("X_REDIRECT_URI", raising=False)
    monkeypatch.delenv("CONNECTOR_VAULT_URL", raising=False)
    main = load_company_os_app()
    monkeypatch.setattr(main.bootstrap_mod, "_mark_owned_auth_from_env", lambda: None)
    from app.content import composio_social
    monkeypatch.setattr(composio_social, "connectable", lambda platform: False)
    # The suite must never exchange live provider credentials or send messages.
    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected live provider HTTP call")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    with company_os_test_client(main.create_app()) as http:
        yield http


def start(client, wid, platform="x", *, subject="usr_alice", body=None):
    path = f"/api/v1/content/oauth/{platform}/start"
    return client.post(path, params={"workspace_id": wid}, json=body or {}, headers=headers(path, "POST", subject))


def state_row(state):
    from app.content.oauth.attempts import digest
    from app.db.models import ContentOAuthState
    from app.state import db
    with db()() as session:
        return session.get(ContentOAuthState, digest(state))


def native_callback(client, state, **params):
    return client.get("/api/v1/content/oauth/x/callback", params={"state": state, "code": "test-code", **params}, follow_redirects=False)


def mock_native(monkeypatch):
    from app.content.oauth import flow
    captured = []
    def exchange(provider, *, form):
        captured.append(form)
        return {"access_token": "provider-access-secret", "refresh_token": "provider-refresh-secret", "expires_in": 3600}
    monkeypatch.setattr(flow, "_exchange", exchange)
    monkeypatch.setattr(flow, "_fetch_identity", lambda *args: {"external_account": "real-provider-id", "handle": "@founder", "author_urn": ""})
    return captured


def test_native_pkce_signed_start_unsigned_callback_and_replay(client, monkeypatch):
    from app.content.oauth import attempts
    from app.db.models import ContentAccount, ContentOAuthState
    from app.state import db
    wid = make_workspace(client, "usr_alice")
    response = start(client, wid)
    assert response.status_code == 200, response.text
    result = response.json()
    row = state_row(result["state"])
    assert row.id != result["state"] and row.actor_subject == "usr_alice" and row.workspace_id == wid
    assert row.code_verifier.startswith("enc:v1:")
    verifier = attempts.verifier({"code_verifier": row.code_verifier})
    assert verifier not in row.code_verifier
    query = parse_qs(urlparse(result["authorize_url"]).query)
    import base64
    import hashlib
    assert query["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")]
    captured = mock_native(monkeypatch)
    callback = native_callback(client, result["state"])
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "https://console.example.test/connections?provider=x&oauth=connected"
    assert callback.headers["cache-control"] == "no-store" and callback.headers["referrer-policy"] == "no-referrer"
    assert captured[0]["code_verifier"] == verifier
    with db()() as session:
        account = session.scalars(select(ContentAccount).where(ContentAccount.workspace_id == wid)).one()
        assert account.access_secret_ref.startswith("vault:")
        assert "provider-access-secret" not in str(account.__dict__)
        consumed = session.get(ContentOAuthState, row.id)
        assert consumed.consumed_at and consumed.code_verifier is None
        account_id = account.id
    from app.content.oauth.flow import access_token
    assert access_token(account_id) == "provider-access-secret"
    assert native_callback(client, result["state"]).status_code == 400
    assert len(captured) == 1


def test_only_exact_get_callbacks_are_independently_authenticated(client):
    for path in ("/api/v1/content/oauth/unknown/callback", "/api/v1/content/oauth/x/callback/extra", "/api/v1/content/oauth/x/start", "/api/v1/ai-gtm/gmail/connect"):
        assert client.get(path).status_code == 401
    assert client.post("/api/v1/content/oauth/x/callback").status_code == 401
    assert client.get("/api/v1/content/oauth/x/callback").status_code == 400
    assert client.get("/api/v1/ai-gtm/gmail/callback").status_code == 400


def test_foreign_founder_and_arbitrary_gmail_identity_cannot_start(client):
    wid = make_workspace(client, "usr_alice")
    make_workspace(client, "usr_bob")
    assert start(client, wid, subject="usr_bob").status_code == 404
    path = "/api/v1/ai-gtm/gmail/connect"
    result = client.post(path, params={"workspace_id": wid}, json={"user_id": "other-founder"}, headers=headers(path, "POST"))
    assert result.status_code == 403


def test_platform_mismatch_and_duplicate_state_do_not_exchange(client, monkeypatch):
    wid = make_workspace(client, "usr_alice")
    state = start(client, wid).json()["state"]
    captured = mock_native(monkeypatch)
    mismatch = client.get("/api/v1/content/oauth/linkedin/callback", params={"state": state, "code": "test-code"}, follow_redirects=False)
    assert mismatch.status_code == 400
    duplicate = client.get("/api/v1/content/oauth/x/callback", params=[("state", state), ("state", "malicious"), ("code", "test-code")])
    assert duplicate.status_code == 400 and not captured
    assert native_callback(client, state).status_code == 303


@pytest.mark.parametrize("failure", ["expired", "membership_removed", "blocked"])
def test_callback_rechecks_lifetime_and_founder_authority(client, monkeypatch, failure):
    from app.content.oauth.attempts import digest
    from app.db.models import ContentOAuthState, Workspace, WorkspaceMember
    from app.state import db
    from core.clock import utcnow
    wid = make_workspace(client, "usr_alice")
    state = start(client, wid).json()["state"]
    with db()() as session:
        if failure == "expired":
            session.get(ContentOAuthState, digest(state)).expires_at = utcnow() - timedelta(minutes=1)
        elif failure == "membership_removed":
            session.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == wid))
        else:
            session.get(Workspace, wid).settings_json = '{"blocked":true}'
        session.commit()
    assert native_callback(client, state).status_code == 400


def test_provider_error_does_not_leak_query_or_claim_success(client, monkeypatch):
    wid = make_workspace(client, "usr_alice")
    state = start(client, wid).json()["state"]
    result = native_callback(client, state, error="denied", error_description="secret@example.test&redirect=https://evil.test")
    assert result.status_code == 303
    assert result.headers["location"] == "https://console.example.test/connections?provider=x&oauth=failed"
    assert "secret@example.test" not in str(result.headers)
    assert native_callback(client, state).status_code == 400


def test_missing_provider_identity_is_a_real_failure(client, monkeypatch):
    from app.content.oauth import flow
    from app.db.models import ContentAccount
    from app.state import db
    wid = make_workspace(client, "usr_alice")
    state = start(client, wid).json()["state"]
    mock_native(monkeypatch)
    monkeypatch.setattr(flow, "_fetch_identity", lambda *args: {"external_account": "", "handle": "", "author_urn": ""})
    result = native_callback(client, state)
    assert result.headers["location"].endswith("oauth=failed")
    with db()() as session:
        assert session.scalars(select(ContentAccount).where(ContentAccount.workspace_id == wid)).all() == []


@pytest.mark.parametrize("return_to", ["//evil.test/connections", "/\\evil.test", "https://evil.test/connections", "/content?next=https://evil.test", "/%2f%2fevil.test"])
def test_return_allowlist(client, return_to):
    wid = make_workspace(client, "usr_alice")
    assert start(client, wid, body={"return_to": return_to}).status_code == 409


def managed_provider(monkeypatch, platform, wid, *, remote_change=None):
    from app.content import composio_social
    from app.gtm.supymem import composio_gmail
    calls = []
    uid = f"gmail:{wid}" if platform == "gmail" else f"content:{wid}"
    toolkit = "twitter" if platform == "x" else platform
    remote = {"id": "ca_test", "status": "ACTIVE", "toolkit": {"slug": toolkit}, "auth_config": {"id": "ac_test", "is_disabled": False}, "experimental": {"account_type": "PRIVATE"}}
    remote.update(remote_change or {})
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "POST" and path == "/connected_accounts/link":
            body = kwargs.get("json") or kwargs.get("json_body")
            assert body["user_id"] == uid
            assert "supymem_state=" in body["callback_url"]
            return {"redirect_url": "https://connect.composio.dev/link-test", "connected_account_id": "ca_test", "link_token": "must-not-leak"}
        assert method == "GET" and path == "/connected_accounts/ca_test"
        return remote
    if platform == "gmail":
        monkeypatch.setattr(composio_gmail, "ensure_auth_config", lambda: "ac_test")
        monkeypatch.setattr(composio_gmail, "_request", request)
        monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"email": "founder@example.test"})
    else:
        monkeypatch.setattr(composio_social, "connectable", lambda p: p == platform)
        monkeypatch.setattr(composio_social, "ensure_auth_config", lambda p: "ac_test")
        monkeypatch.setattr(composio_social, "_request", request)
        monkeypatch.setattr(composio_social, "enrich_profile", lambda **kwargs: {"external_account": "social-profile", "handle": "founder", "author_urn": ""})
    return calls


def managed_start(client, wid, platform):
    if platform != "gmail":
        return start(client, wid, platform).json()
    path = "/api/v1/ai-gtm/gmail/connect"
    response = client.post(path, params={"workspace_id": wid}, json={}, headers=headers(path, "POST"))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("platform", ["linkedin", "reddit", "x", "gmail"])
def test_managed_receipt_binding_and_real_scoped_sync(client, monkeypatch, platform):
    from app.db.models import ContentAccount, GtmEmailAccount
    from app.state import db
    wid = make_workspace(client, "usr_alice")
    monkeypatch.setenv("COMPOSIO_CONTENT_USER_ID", "dangerous-global-user")
    monkeypatch.setenv("SUPYMEM_DEFAULT_USER_ID", "dangerous-global-user")
    calls = managed_provider(monkeypatch, platform, wid)
    result = managed_start(client, wid, platform)
    assert "must-not-leak" not in str(result)
    state = state_row(result["state"])
    assert state.connected_account_id == "ca_test" and state.auth_config_id == "ac_test"
    path = urlparse(result["redirect_uri"]).path
    callback = client.get(path, params={"supymem_state": result["state"], "connectedAccountId": "foreign-id"}, follow_redirects=False)
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"].endswith(f"provider={platform}&oauth=connected")
    model = GtmEmailAccount if platform == "gmail" else ContentAccount
    with db()() as session:
        account = session.scalars(select(model).where(model.workspace_id == wid)).one()
        assert account.status == "active"
    assert len(calls) == 2
    assert client.get(path, params={"supymem_state": result["state"]}).status_code == 400


@pytest.mark.parametrize("remote_change", [
    {"id": "foreign-id"}, {"user_id": "foreign-user"}, {"toolkit": {"slug": "gmail"}},
    {"auth_config": {"id": "foreign-config"}}, {"status": "INITIATED"},
    {"experimental": {"account_type": "project-scoped"}},
])
def test_managed_remote_binding_mismatch_fails_without_import(client, monkeypatch, remote_change):
    from app.db.models import ContentAccount
    from app.state import db
    wid = make_workspace(client, "usr_alice")
    managed_provider(monkeypatch, "linkedin", wid, remote_change=remote_change)
    result = managed_start(client, wid, "linkedin")
    callback = client.get(urlparse(result["redirect_uri"]).path, params={"supymem_state": result["state"]}, follow_redirects=False)
    assert callback.headers["location"].endswith("oauth=failed")
    with db()() as session:
        assert not session.scalars(select(ContentAccount).where(ContentAccount.workspace_id == wid)).all()


def test_unbound_toolkit_lists_never_import_other_founders(client, monkeypatch):
    from app.content import composio_social
    from app.gtm.supymem import composio_gmail
    items = [{"id": "unbound", "status": "ACTIVE", "toolkit": {"slug": "linkedin"}},
             {"id": "foreign", "status": "ACTIVE", "toolkit": {"slug": "linkedin"}, "user_id": "foreign"}]
    assert composio_social._normalize_connection_items(items, uid="ours", platform=None, active_only=True) == []
    monkeypatch.setattr(composio_gmail, "_request", lambda *args, **kwargs: {"items": items})
    assert composio_gmail.list_gmail_connections("ours") == []


def test_successful_receipt_reconnects_when_provider_omits_user_id(client, monkeypatch):
    from app.content import composio_social
    from app.content.oauth.callbacks import known_connections
    wid = make_workspace(client, "usr_alice")
    managed_provider(monkeypatch, "linkedin", wid)
    started = managed_start(client, wid, "linkedin")
    response = client.get(urlparse(started["redirect_uri"]).path, params={"supymem_state": started["state"]}, follow_redirects=False)
    assert response.headers["location"].endswith("oauth=connected")
    known = known_connections(wid, platform="linkedin")
    assert [item["id"] for item in known] == ["ca_test"]
    other = make_workspace(client, "usr_bob")
    assert known_connections(other, platform="linkedin") == []
    original = composio_social._request
    monkeypatch.setattr(composio_social, "_request", lambda method, path, **kwargs: {"items": []} if path == "/connected_accounts" else original(method, path, **kwargs))
    synced = composio_social.sync_workspace(wid, platform="linkedin")
    assert len(synced) == 1 and synced[0]["status"] == "active"
