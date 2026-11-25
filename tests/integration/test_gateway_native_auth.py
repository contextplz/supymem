"""Native PKCE browser return, explicit approval, and revocable bearer access."""

import importlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.responses import JSONResponse

from tests.integration.test_gateway_b1_security import _close_gateway, _gateway


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNITO_DOMAIN", "https://auth.supymem.com")
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        yield main, client
    finally:
        _close_gateway(saved)


def test_cognito_domain_is_env_driven_and_missing_configuration_fails_closed(gateway, monkeypatch):
    main, client = gateway
    from app.config import Settings

    monkeypatch.delenv("COGNITO_DOMAIN")
    # Production sets no COGNITO_DOMAIN: the default must still sign founders in.
    assert Settings(_env_file=None).cognito_domain == "https://selfhost-prod-auth.auth.us-east-1.amazoncognito.com"
    monkeypatch.setenv("COGNITO_DOMAIN", "https://auth.supymem.com")
    assert Settings(_env_file=None).cognito_domain == "https://auth.supymem.com"
    monkeypatch.setattr(main.settings, "cognito_domain", "")
    response = client.post("/auth/cognito/start")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_unavailable"
    assert "Cognito" not in response.text
    assert "authorize_url" not in response.json()
    assert not any("cognito" in name for name in client.cookies)
    assert client.post("/auth/logout").json()["upstream_logout_url"] == ""


async def test_cognito_provider_failure_returns_neutral_sign_in_copy(gateway, monkeypatch):
    from app import cognito

    async def unavailable(*args, **kwargs):
        raise cognito.httpx.ConnectError("Cannot connect to Cognito in us-east-1")

    monkeypatch.setattr(cognito.httpx.AsyncClient, "post", unavailable)
    with pytest.raises(cognito.CognitoError) as failure:
        await cognito.exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert failure.value.code == "identity_unavailable"
    assert str(failure.value) == "Supymem sign-in is temporarily unavailable. Try again shortly."


def test_unauthenticated_browser_resumes_at_fixed_console(gateway):
    main, client = gateway
    state = "s" * 43
    response = client.get("/auth/device/approve", params={
        "code_challenge": "c" * 43, "state": state,
        "device_name": "Test Mac", "return_to": "https://attacker.example",
    }, follow_redirects=False)
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}" == main.settings.console_origin
    assert target.path == "/auth/device"
    assert parse_qs(target.query)["state"] == [state]
    assert "return_to" not in parse_qs(target.query)
    assert response.headers["cache-control"] == "no-store"


def test_approval_requires_csrf_and_round_trips_state(gateway):
    main, client = gateway
    login = client.post("/auth/login", json={"passphrase": "changeme"})
    state = "s" * 43
    body = {"code_challenge": "c" * 43, "state": state}
    assert client.post("/auth/device/approve", json=body).status_code == 403
    headers = {"X-CSRF-Token": login.json()["csrf"], "Origin": main.settings.gateway_origin}
    approved = client.post("/auth/device/approve", json=body, headers=headers)
    assert approved.status_code == 200
    callback = urlsplit(approved.json()["redirect"])
    assert callback.scheme == "supymem" and callback.netloc == "auth" and callback.path == "/callback"
    assert parse_qs(callback.query) == {"code": [approved.json()["code"]], "state": [state]}
    assert approved.headers["cache-control"] == "no-store"
    assert client.post("/auth/device/approve", json=body, headers={**headers, "Origin": "https://attacker.example"}).status_code == 403


@pytest.mark.parametrize("google", [False, True])
def test_federated_exchange_persists_verified_profile_without_password_account(gateway, monkeypatch, google):
    from app import cognito, proxy, users

    main, client = gateway
    identity = cognito.CognitoIdentity(sub="persistent", email="same@example.test",
        email_verified=not google, trusted_google_identity=google)

    async def exchange(**kwargs):
        return identity

    async def admit(*args):
        pass

    monkeypatch.setattr(cognito, "exchange_code", exchange)
    monkeypatch.setattr(proxy, "admit_verified_account", admit)
    users.create_user("same@example.test", "password1234")
    start = client.post("/auth/cognito/start")
    state = parse_qs(urlsplit(start.json()["authorize_url"]).query)["state"][0]
    response = client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state})
    assert response.status_code == 200, response.text
    subject = cognito.founder_subject(identity.sub)
    profile = users.get_user(subject)
    assert profile.email == identity.email and profile.email_verified
    password_user = users.get_user_by_email(identity.email)
    assert password_user.id != subject and not password_user.email_verified
    assert users.verify_credentials(identity.email, "password1234").id == password_user.id
    users.record_verified_identity(subject, "updated@example.test")
    assert users.get_user(subject).email == "updated@example.test"
    assert users.get_user(subject).created_at == profile.created_at
    assert proxy._session_email(subject) == "updated@example.test"


async def test_old_federated_cookie_approval_repairs_device_email_lookup(gateway, monkeypatch):
    import httpx
    from starlette.requests import Request
    from core.assertion import MIN_SECRET_BYTES, verify_assertion
    from app import device_auth, proxy, users
    from app.auth import set_session_cookie
    from app.session import issue_session
    from fastapi import Response

    main, client = gateway
    subject = "usr_cog_oldcookie"
    token, session = issue_session(subject, email="device@example.test")
    response = Response()
    set_session_cookie(response, token)
    for header in response.headers.getlist("set-cookie"):
        name, value = header.split(";", 1)[0].split("=", 1)
        client.cookies.set(name, value)
    csrf = client.get("/auth/session").json()["csrf"]
    verifier = "v" * 43
    approved = client.post("/auth/device/approve", json={"code_challenge": device_auth._s256(verifier)},
        headers={"X-CSRF-Token": csrf, "Origin": main.settings.gateway_origin})
    assert approved.status_code == 200, approved.text
    assert users.get_user(subject).email == "device@example.test"
    exchanged = device_auth.exchange_code(code=approved.json()["code"], code_verifier=verifier)
    bearer = device_auth.load_bearer(exchanged[2])
    assert bearer.subject == subject and not bearer.email
    secret = "e" * MIN_SECRET_BYTES
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    seen = []

    def handler(request):
        seen.append(verify_assertion(secret, request.headers["x-cos-assertion"], path="/profile", method="GET"))
        return httpx.Response(200, json={"ok": True})

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))

    async def receive():
        return {"type": "http.request", "body": b""}

    request = Request({"type": "http", "method": "GET", "path": "/profile", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("testserver", 80), "client": ("testclient", 50000)}, receive)
    assert (await proxy.forward("/profile", request, bearer)).status_code == 200
    assert seen[0].email == "device@example.test"


def test_local_approval_page_hides_production_app_link(gateway, monkeypatch):
    main, client = gateway
    monkeypatch.setattr(main.settings, "app_env", "local")
    assert client.post("/auth/login", json={"passphrase": "changeme"}).status_code == 200
    page = client.get("/auth/device/approve", params={"code_challenge": "c" * 43})
    assert page.status_code == 200
    assert "const localManualCode=true" in page.text
    assert ".fallback-link[hidden] { display:none; }" in page.text


def test_cognito_hosted_login_issues_gateway_session_and_binds_callback_state(gateway, monkeypatch):
    main, client = gateway
    from app import cognito

    async def exchange_code(*, code, verifier):
        assert code == "authorization-code"
        assert len(verifier) >= 43
        return cognito.CognitoIdentity(sub="cognito-subject", email="founder@example.com", email_verified=True)

    monkeypatch.setattr(cognito, "exchange_code", exchange_code)
    start = client.post("/auth/cognito/start")
    assert start.status_code == 200
    target = urlsplit(start.json()["authorize_url"])
    assert target.netloc == "auth.supymem.com"
    state = parse_qs(target.query)["state"][0]
    assert target.scheme == "https"
    assert "code_challenge" in parse_qs(target.query)
    assert parse_qs(target.query)["prompt"] == ["login select_account"]
    complete = client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state})
    assert complete.status_code == 200
    assert complete.json()["mode"] == "cognito"
    assert complete.json()["subject"] == cognito.founder_subject("cognito-subject")
    assert complete.json()["email"] == "founder@example.com"
    assert client.get("/auth/session").json()["email"] == "founder@example.com"
    assert client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state}).status_code == 400
    approved = client.post(
        "/auth/device/approve",
        json={"code_challenge": "c" * 43},
        headers={"X-CSRF-Token": complete.json()["csrf"], "Origin": main.settings.gateway_origin},
    )
    assert approved.status_code == 200


def test_direct_google_login_forces_the_google_account_chooser(gateway):
    _main, client = gateway
    start = client.post("/auth/cognito/start?provider=google")
    assert start.status_code == 200
    target = urlsplit(start.json()["authorize_url"])
    query = parse_qs(target.query)
    assert target.path == "/oauth2/authorize"
    assert query["identity_provider"] == ["Google"]
    assert query["prompt"] == ["login select_account"]


def test_logout_clears_gateway_session_and_ends_cognito_sso(gateway):
    main, client = gateway

    login = client.post("/auth/login", json={"passphrase": "changeme"})
    assert login.status_code == 200
    response = client.post("/auth/logout")
    assert response.status_code == 200
    target = urlsplit(response.json()["upstream_logout_url"])
    assert f"{target.scheme}://{target.netloc}" == main.settings.cognito_domain
    assert target.path == "/logout"
    assert parse_qs(target.query) == {
        "client_id": [main.settings.cognito_client_id],
        "logout_uri": [f"{main.settings.console_origin.rstrip('/')}/account"],
    }
    session = client.get("/auth/session")
    assert session.status_code == 200
    assert session.json()["authenticated"] is False


def test_cognito_exchange_rejection_clears_the_one_time_callback_cookie(gateway, monkeypatch):
    _main, client = gateway
    from app import cognito

    async def reject_code(*, code, verifier):
        raise cognito.CognitoError("email_not_verified", "Cognito did not verify this email.")

    monkeypatch.setattr(cognito, "exchange_code", reject_code)
    start = client.post("/auth/cognito/start")
    state = parse_qs(urlsplit(start.json()["authorize_url"]).query)["state"][0]
    rejected = client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state})
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "email_not_verified"
    assert "Max-Age=0" in rejected.headers["set-cookie"]


def test_cognito_accepts_a_cognito_signed_google_identity_when_legacy_email_flag_is_false(gateway, monkeypatch):
    _main, client = gateway
    from app import cognito

    async def exchange_code(*, code, verifier):
        return cognito.CognitoIdentity(
            sub="google-subject", email="founder@example.com", email_verified=False,
            trusted_google_identity=True,
        )

    monkeypatch.setattr(cognito, "exchange_code", exchange_code)
    start = client.post("/auth/cognito/start")
    state = parse_qs(urlsplit(start.json()["authorize_url"]).query)["state"][0]
    complete = client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state})
    assert complete.status_code == 200
    assert complete.json()["mode"] == "cognito"


def test_cognito_recognizes_only_the_signed_google_federation_claim_shapes():
    from app.cognito import _trusted_google_identity

    assert _trusted_google_identity({"cognito:username": "Google_123"}) is True
    assert _trusted_google_identity({"identities": '[{"providerName":"Google","providerType":"Google"}]'}) is True
    assert _trusted_google_identity({"cognito:username": "local-founder"}) is False


def test_staging_operator_passphrase_runs_the_existing_pkce_flow(gateway, monkeypatch):
    """The staging fixture is a fixed canonical subject, never client input."""
    main, client = gateway
    monkeypatch.setattr(main.settings, "app_env", "staging")
    monkeypatch.setattr(main.settings, "staging_operator_passphrase_login_enabled", True)
    monkeypatch.setattr(main.settings, "staging_operator_subject", "usr_staging_operator")

    login = client.post("/auth/login", json={
        "passphrase": "changeme",
        # LoginRequest ignores extras; they must not change the operator identity.
        "subject": "usr_attacker",
    })
    assert login.status_code == 200
    assert login.json()["subject"] == "usr_staging_operator"
    assert login.json()["mode"] == "staging_operator_passphrase"

    verifier = "v" * 43
    devices = importlib.import_module("app.device_auth")
    approved = client.post(
        "/auth/device/approve",
        json={"code_challenge": devices._s256(verifier), "device_name": "Staging Mac"},
        headers={"X-CSRF-Token": login.json()["csrf"], "Origin": main.settings.gateway_origin},
    )
    assert approved.status_code == 200
    exchanged = client.post("/auth/device/token", json={
        "code": approved.json()["code"], "code_verifier": verifier,
    })
    assert exchanged.status_code == 200
    assert exchanged.json()["subject"] == "usr_staging_operator"
    bearer = {"Authorization": f"Bearer {exchanged.json()['access_token']}"}
    assert client.get("/auth/session", headers=bearer).json()["subject"] == "usr_staging_operator"
    # The staging fixture is not a fabricated verified customer account; Core
    # will materialize only this subject's scoped Person/desk on first use.
    from app import users

    assert users.get_user("usr_staging_operator") is None
    proof = {"device_id": exchanged.json()["device_id"], "refresh_token": exchanged.json()["refresh_token"]}
    assert client.post("/auth/device/revoke", json=proof).json() == {"revoked": True}
    assert client.get("/auth/session", headers=bearer).status_code == 401


def test_staging_operator_flag_rejects_non_staging_boot(gateway, monkeypatch):
    main, _client = gateway
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "staging_operator_passphrase_login_enabled", True)
    monkeypatch.setattr(main.settings, "staging_operator_subject", "usr_staging_operator")
    with pytest.raises(RuntimeError, match="only permitted in staging"):
        main.create_app()


def _tokens(client):
    devices = importlib.import_module("app.device_auth")
    device = devices.create_device(person_id="founder", name="Native test Mac")
    verifier = "v" * 43
    code = devices.store_auth_code(person_id="founder", device_id=device, code_challenge=devices._s256(verifier))
    response = client.post("/auth/device/token", json={"code": code, "code_verifier": verifier})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("environment,scheme", [("production", "supymem"), ("staging", "selfhost-staging")])
def test_approval_callback_is_bound_to_server_environment(gateway, monkeypatch, environment, scheme):
    main, client = gateway
    signup = client.post("/auth/signup", json={"email": "native@example.com", "password": "test-password-1234"})
    assert signup.status_code == 200
    monkeypatch.setattr(main.settings, "app_env", environment)
    approved = client.post("/auth/device/approve", json={
        "code_challenge": "c" * 43, "state": "s" * 43,
        "redirect_uri": "attacker://auth/callback",
    }, headers={"X-CSRF-Token": signup.json()["csrf"], "Origin": main.settings.gateway_origin})
    assert approved.status_code == 200
    callback = urlsplit(approved.json()["redirect"])
    assert (callback.scheme, callback.netloc, callback.path) == (scheme, "auth", "/callback")


def test_bearer_mutation_and_logout_without_browser_cookie(gateway, monkeypatch):
    main, client = gateway
    tokens = _tokens(client)

    async def forwarded(path, request, session):
        assert session.subject == "founder"
        return JSONResponse({"accepted": True})

    monkeypatch.setattr(main, "forward", forwarded)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    client.cookies.clear()
    assert client.post("/api/workspaces/ws_test/tasks", headers=headers, json={}).status_code == 200
    bad = {"device_id": tokens["device_id"], "refresh_token": "not-the-device-token-but-long-enough"}
    assert client.post("/auth/device/revoke", json=bad).status_code == 401
    assert client.get("/auth/session", headers=headers).json()["authenticated"] is True
    proof = {"device_id": tokens["device_id"], "refresh_token": tokens["refresh_token"]}
    for _ in range(2):
        assert client.post("/auth/device/revoke", json=proof).json() == {"revoked": True}
    assert client.get("/auth/session", headers=headers).status_code == 401
    assert client.post("/auth/device/refresh", json=proof).status_code == 401
    assert client.post("/api/workspaces/ws_test/tasks", headers=headers, json={}).status_code == 401


def test_foreign_device_proof_cannot_revoke_another_mac(gateway):
    _, client = gateway
    first, second = _tokens(client), _tokens(client)
    mixed = {"device_id": first["device_id"], "refresh_token": second["refresh_token"]}
    assert client.post("/auth/device/revoke", json=mixed).status_code == 401
    for tokens in (first, second):
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert client.get("/auth/session", headers=headers).json()["authenticated"] is True


@pytest.mark.parametrize("key,value", [("state", "<script>alert(1)</script>"), ("code_challenge", "é" * 43)])
def test_invalid_native_parameters_are_rejected_before_redirect(gateway, key, value):
    _, client = gateway
    body = {"code_challenge": "c" * 43, "state": "s" * 43, key: value}
    assert client.get("/auth/device/approve", params=body, follow_redirects=False).status_code == 422


def test_unicode_verifier_is_validation_error(gateway):
    _, client = gateway
    result = client.post("/auth/device/token", json={"code": "c" * 43, "code_verifier": "é" * 43})
    assert result.status_code == 422


def test_native_signing_key_rejects_invalid_or_wrong_curve(gateway, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    devices = importlib.import_module("app.device_auth")
    monkeypatch.setenv("JWT_SIGNING_KEY_ES256", "not-a-private-key")
    with pytest.raises(RuntimeError, match="valid P-256"):
        devices._private_key()
    for curve in (ec.SECP384R1(), ec.SECP256R1()):
        key = ec.generate_private_key(curve)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        monkeypatch.setenv("JWT_SIGNING_KEY_ES256", pem)
        if isinstance(curve, ec.SECP384R1):
            with pytest.raises(RuntimeError, match="valid P-256"):
                devices._private_key()
        else:
            assert isinstance(devices._private_key().curve, ec.SECP256R1)


def test_cognito_first_name_from_given_name_then_name_and_sanitized(gateway):
    from app.cognito import clean_first_name, first_name_from_claims

    assert first_name_from_claims({"given_name": "Sai", "name": "Other Person"}) == "Sai"
    assert first_name_from_claims({"name": "Avery Chen"}) == "Avery"
    assert first_name_from_claims({"given_name": "<b>", "name": "Ann Lee"}) == "Ann"
    assert first_name_from_claims({"email": "a@b.co"}) is None
    assert clean_first_name("  Mary-Jane ") == "Mary-Jane"
    assert clean_first_name("O'Brien") == "O'Brien"
    for bad in ["", "   ", "x" * 41, "A1", "a@b", "Bob<script>", None, 5]:
        assert clean_first_name(bad) is None


def test_cognito_exchange_pushes_first_name_to_core(gateway, monkeypatch):
    main, client = gateway
    from app import cognito, proxy

    async def exchange_code(*, code, verifier):
        return cognito.CognitoIdentity(sub="s1", email="f@example.com", email_verified=True, first_name="Ada")

    pushed = []

    async def push(subject, email, name):
        pushed.append((subject, name))

    monkeypatch.setattr(cognito, "exchange_code", exchange_code)
    monkeypatch.setattr(proxy, "push_first_name", push)
    state = parse_qs(urlsplit(client.post("/auth/cognito/start").json()["authorize_url"]).query)["state"][0]
    assert client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state}).status_code == 200
    assert pushed == [(cognito.founder_subject("s1"), "Ada")]


def test_cognito_admission_failure_does_not_issue_gateway_session(gateway, monkeypatch):
    from fastapi import HTTPException
    main, client = gateway
    from app import cognito, proxy
    seen = []
    async def exchange_code(*, code, verifier):
        return cognito.CognitoIdentity(sub="new-invited-user", email="new@example.test", email_verified=True)
    async def refuse(subject, email, invite_code=None, legacy_created_at=None):
        seen.append((subject, invite_code))
        raise HTTPException(403, detail={"code": "invite_code_exhausted"})
    monkeypatch.setattr(cognito, "exchange_code", exchange_code)
    monkeypatch.setattr(proxy, "admit_verified_account", refuse)
    start = client.post("/auth/cognito/start")
    state = parse_qs(urlsplit(start.json()["authorize_url"]).query)["state"][0]
    result = client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state, "invite_code": "USEDUP22"})
    assert result.status_code == 403
    assert seen == [(cognito.founder_subject("new-invited-user"), "USEDUP22")]
    assert client.get("/auth/session").json()["authenticated"] is False


def test_cognito_invite_survives_storage_loss_bound_to_its_pending_flow(gateway, monkeypatch):
    _, client = gateway
    from app import cognito, proxy
    seen = []
    async def exchange_code(*, code, verifier):
        return cognito.CognitoIdentity(sub="invite-flow", email="invited@example.test", email_verified=True)
    async def admit(subject, email, invite_code=None, legacy_created_at=None):
        seen.append(invite_code)
    monkeypatch.setattr(cognito, "exchange_code", exchange_code)
    monkeypatch.setattr(proxy, "admit_verified_account", admit)
    first = client.post("/auth/cognito/start", json={"invite_code": "OLDUSER2"})
    old_state = parse_qs(urlsplit(first.json()["authorize_url"]).query)["state"][0]
    latest = client.post("/auth/cognito/start", json={"invite_code": "NEWUSER2"})
    state = parse_qs(urlsplit(latest.json()["authorize_url"]).query)["state"][0]
    assert client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": old_state}).status_code == 400
    # The invalid old callback cleared its cookie; a new reviewed flow is needed.
    latest = client.post("/auth/cognito/start", json={"invite_code": "NEWUSER2"})
    state = parse_qs(urlsplit(latest.json()["authorize_url"]).query)["state"][0]
    result = client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state, "invite_code": "STALE222"})
    assert result.status_code == 200 and seen == ["NEWUSER2"]
    assert "cos_cognito_pending" not in client.cookies


def _stub_download(main, monkeypatch):
    from fastapi.responses import RedirectResponse
    async def fake_forward(path, request, **kwargs):
        return RedirectResponse("https://example.test/Supymem.dmg", status_code=302)
    monkeypatch.setattr(main, "forward", fake_forward)


def _cognito_flow(client, monkeypatch, seen, sub="dl-user"):
    from app import cognito, proxy
    async def exchange_code(*, code, verifier):
        return cognito.CognitoIdentity(sub=sub, email="dl@example.test", email_verified=True)
    async def admit(subject, email, invite_code=None, legacy_created_at=None):
        seen.append(invite_code)
    monkeypatch.setattr(cognito, "exchange_code", exchange_code)
    monkeypatch.setattr(proxy, "admit_verified_account", admit)
    start = client.post("/auth/cognito/start")
    return parse_qs(urlsplit(start.json()["authorize_url"]).query)["state"][0]


def test_code_entered_at_download_is_used_at_sign_in_without_asking_again(gateway, monkeypatch):
    main, client = gateway
    _stub_download(main, monkeypatch)
    assert client.get("/auth/invite/status").json() == {"pending": False}
    assert client.get("/public/beta/download?code=abcd2345", follow_redirects=False).status_code == 302
    status = client.get("/auth/invite/status")
    assert status.json() == {"pending": True} and "ABCD2345" not in status.text
    seen = []
    state = _cognito_flow(client, monkeypatch, seen)
    assert client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state}).status_code == 200
    assert seen == ["ABCD2345"]  # no code typed at sign-in
    assert client.get("/auth/invite/status").json() == {"pending": False}  # consumed: replay not possible


def test_typed_code_wins_and_no_code_anywhere_stays_none(gateway, monkeypatch):
    main, client = gateway
    _stub_download(main, monkeypatch)
    seen = []
    state = _cognito_flow(client, monkeypatch, seen)
    assert client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state}).status_code == 200
    assert seen == [None]  # no code anywhere: sign-in still succeeds
    client.get("/public/beta/download?code=ABCD2345", follow_redirects=False)
    state = _cognito_flow(client, monkeypatch, seen)
    client.post("/auth/cognito/exchange", json={"code": "authorization-code", "state": state, "invite_code": "TYPED222"})
    assert seen[-1] == "TYPED222"


def test_forged_or_failed_download_cookie_is_ignored(gateway, monkeypatch):
    main, client = gateway
    client.cookies.set("cos_invite", "ABCD2345")  # unsigned
    assert client.get("/auth/invite/status").json() == {"pending": False}
    from fastapi.responses import JSONResponse
    async def refuse(path, request, **kwargs):
        return JSONResponse({"ok": False}, status_code=404)
    monkeypatch.setattr(main, "forward", refuse)
    client.cookies.clear()
    client.get("/public/beta/download?code=ABCD2345")
    assert client.get("/auth/invite/status").json() == {"pending": False}


def test_mac_approval_auto_only_for_the_challenge_bound_before_sign_in(gateway):
    main, client = gateway
    mine, other = {"code_challenge": "c" * 43}, {"code_challenge": "d" * 43}
    # Signed out: the approve page sends the browser to sign in and binds this challenge.
    assert client.get("/auth/device/approve", params=mine, follow_redirects=False).status_code == 303
    client.post("/auth/login", json={"passphrase": "changeme"})
    assert "const autoApprove=false" in client.get("/auth/device/approve", params=other).text  # attacker link
    assert "const autoApprove=true" in client.get("/auth/device/approve", params=mine).text
    # Approving consumes both markers: the same challenge is manual afterwards.
    csrf = client.cookies.get("cos_csrf")
    client.post("/auth/device/approve", json=mine, headers={"X-CSRF-Token": csrf, "Origin": main.settings.gateway_origin})
    assert "const autoApprove=false" in client.get("/auth/device/approve", params=mine).text


def test_mac_approval_never_auto_without_binding_or_with_expired_binding(gateway, monkeypatch):
    main, client = gateway
    params = {"code_challenge": "c" * 43}
    client.post("/auth/login", json={"passphrase": "changeme"})  # fresh login, nothing bound
    assert "const autoApprove=false" in client.get("/auth/device/approve", params=params).text
    import time
    client.cookies.clear()
    client.get("/auth/device/approve", params=params, follow_redirects=False)
    client.post("/auth/login", json={"passphrase": "changeme"})
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 301)
    client.cookies.set("cos_fresh", "1")  # isolate: only the binding is expired
    assert "const autoApprove=false" in client.get("/auth/device/approve", params=params).text


@pytest.mark.parametrize("outcome", [403, 503, "down"])
async def test_beta_admission_refusal_or_outage_never_blocks_sign_in(gateway, monkeypatch, outcome):
    """Pre-gate Google accounts have no admission row, so Core answers invite_code_required."""
    import httpx
    from app import proxy
    monkeypatch.setenv("SUPYMEM_REFERRAL_ADMISSION_REQUIRED", "1")
    monkeypatch.setattr(proxy, "_assertion_secret", lambda: "s" * 64)

    def handler(request):
        if outcome == "down":
            raise httpx.ConnectError("core down")
        return httpx.Response(outcome, json={"detail": {"code": "invite_code_required"}})
    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert await proxy.admit_verified_account("usr_existing", "old@example.test") is None


async def test_proxy_preserves_cognito_session_email_only_for_the_bound_user(gateway, monkeypatch):
    import httpx
    from starlette.requests import Request
    from app import proxy
    from app.session import issue_session
    from core.assertion import verify_assertion

    secret = "s" * 64
    monkeypatch.setattr(proxy, "_assertion_secret", lambda: secret)
    monkeypatch.setattr(proxy, "_session_email", lambda _subject: "")
    seen = []

    def handler(request):
        claims = verify_assertion(secret, request.headers["X-COS-Assertion"], path="/profile", method="GET")
        seen.append(claims)
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))

    async def receive():
        return {"type": "http.request", "body": b""}

    request = Request({"type": "http", "method": "GET", "path": "/profile", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("testserver", 80), "client": ("testclient", 50000)}, receive)
    _, session = issue_session("usr_cog_synthetic", email="founder@example.test")
    for overrides in ({}, {"subject": "usr_other"}, {"role": "admin"}):
        assert (await proxy.forward("/profile", request, session, **overrides)).status_code == 200
    assert [claims.email for claims in seen] == ["founder@example.test", "", ""]


@pytest.mark.parametrize("failure,status,code", [("timeout", 504, "upstream_timeout"), ("connection", 503, "core_restarting")])
async def test_proxy_distinguishes_timeout_from_connection_failure(gateway, monkeypatch, failure, status, code):
    import httpx
    from starlette.requests import Request
    from app import proxy
    def handler(request):
        exception = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
        raise exception("Synthetic transport failure", request=request)
    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    async def receive():
        return {"type": "http.request", "body": b""}
    path = "/api/v1/ventures/ven_fixture/gtm/journey"
    request = Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [],
        "scheme": "http", "server": ("testserver", 80), "client": ("testclient", 50000)}, receive)
    response = await proxy.forward(path, request, None)
    import json
    payload = json.loads(response.body)
    assert response.status_code == status and payload.get("error", payload)["code"] == code
