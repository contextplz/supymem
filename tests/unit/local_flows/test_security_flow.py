"""Local owners get working features without opening their owner session to the web."""

import asyncio
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import traceback
from contextlib import contextmanager
from html import escape
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.unit.test_local_acceptance import acceptance
from tests.unit.test_local_mode import local_owner_app


@pytest.fixture(params=[
    pytest.param(("A scheduling tool for independent dental laboratories", "Dental laboratory coordinators",
                  "laboratory.example"), id="b2b"),
    pytest.param(("A trail journal for recreational birdwatchers", "Recreational birdwatchers",
                  "birdwatching.example"), id="consumer"),
])
def scenario(request):
    return request.param


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    monkeypatch.setenv("GUESSED_EMAILS", "off")
    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "0")
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", "security-flow")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(tmp_path / "import.env"))
    monkeypatch.delenv("REDIS_URL", raising=False)
    state = SimpleNamespace(requests=[], logins=[], sent=[], redirect=None, addresses={}, failure=False,
                            forbidden_host=False)

    def resolve(host, port, *args, **kwargs):
        addresses = state.addresses.get(host)
        if addresses is None:
            try:
                addresses = [str(ipaddress.ip_address(host))]
            except ValueError:
                addresses = ["93.184.216.34"]
        return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM,
                 socket.IPPROTO_TCP, "", (address, port or 443)) for address in addresses]

    def transport(request):
        host = request.headers.get("host", request.url.host).split(":")[0]
        state.requests.append((request.method, request.url.host, host, request.url.path))
        state.forbidden_host |= any(value.lower().rstrip(".") == "supymem.com" or
                                    value.lower().rstrip(".").endswith(".supymem.com")
                                    for value in (host, request.url.host))
        assert not state.forbidden_host, "Local work must never contact a Supymem host"
        if state.failure:
            return httpx.Response(401, text="Provider rejected " + state.secret)
        if request.url.host == "dns.google":
            return httpx.Response(200, json={"Status": 3})
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [{"title": "Public source", "url": state.source,
                "content": "Public information about the audience, with a contact page."}]})
        if request.url.path == "/robots.txt":
            return httpx.Response(200, stream=httpx.ByteStream(b"User-agent: *\nAllow: /\n"))
        if state.redirect and request.url.path == "/source":
            return httpx.Response(302, headers={"location": state.redirect})
        if request.url.host in {"api.vercel.com", "api.netlify.com"}:
            return httpx.Response(200, json={"user": {"id": "test-account"}})
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream((
            "<html><head><title>Public source</title></head><body><main>"
            "This public page describes the audience and its existing workflows. " * 8 + "</main></body></html>").encode("utf-8")))

    real_sync, real_async = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_sync(**(kwargs | {
        "transport": httpx.MockTransport(transport)})))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_async(**(kwargs | {
        "transport": httpx.MockTransport(transport)})))
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("Unexpected socket connection"))
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: pytest.fail("Unexpected socket connection"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *args, **kwargs: pytest.fail("Unexpected socket connection"))

    class MailTransport:
        def __init__(self, host, *args, **kwargs):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, **kwargs):
            return 220, b"ready"

        def login(self, user, password):
            state.logins.append((self.host, user))
            if state.failure:
                import smtplib
                raise smtplib.SMTPAuthenticationError(535, ("Rejected " + password).encode())
            return "OK", [b"authenticated"]

        def send_message(self, message, **kwargs):
            state.sent.append(message)
            return {}

    import imaplib
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", MailTransport)
    monkeypatch.setattr(smtplib, "SMTP_SSL", MailTransport)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", MailTransport)
    yield state
    assert not state.forbidden_host, "A caught provider error must not conceal a Supymem request"


@pytest.fixture
def owner(boundary, local_owner_app, scenario, monkeypatch, tmp_path):
    """Isolate HTTP security from disk migrations, which have their own startup regression."""
    from app.core.local_env_import import _FIELDS
    from app.core.providers_local import PROVIDER_ENV
    from core.local_security import _proxy_secret
    from app.db import session as database
    from sqlalchemy.pool import NullPool

    # shortcut: schema bootstraps without file migrations, use the ordinary file URL once SEC-001 is fixed.
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setattr(database, "_sqlite_connect_kwargs", lambda url: {
        "poolclass": NullPool, "creator": lambda: sqlite3.connect(tmp_path / "flow.db",
            check_same_thread=False, timeout=30)})
    for name in set(_FIELDS) | {name for names in PROVIDER_ENV.values() for name in names} | {"SUPYMEM_LLM_PROVIDER"}:
        if name not in {"LLM_OFFLINE", "LLM_CASSETTE_MODE"}:
            monkeypatch.delenv(name, raising=False)
    with TestClient(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        credential = _proxy_secret().decode("ascii")
        client.headers["X-Local-Proxy-Secret"] = credential
        assert client.post("/local/providers/sample", json={}).status_code == 200
        venture = client.post("/ventures", json={"raw_idea": scenario[0], "start": False})
        assert venture.status_code == 201, venture.text
        client.headers["X-Workspace-Id"] = venture.json()["workspace_id"]
        boundary.source = f"https://{scenario[2]}/source"
        yield SimpleNamespace(http=client, app=local_owner_app, venture=venture.json(),
                              path=f"/ventures/{venture.json()['id']}", credential=credential, boundary=boundary)


def drain_jobs():
    """Use the production lease/worker loop, not a shortcut through job handlers."""
    from app.state import db
    from app.worker.main import run_worker
    from app.db.models import Job
    from app.core.jobs import CLAIMABLE, _due
    from app.credits._txn import naive_utc
    from sqlalchemy import select
    from app.worker.catalog import enabled_kinds

    for _ in range(12):
        with db()() as session:
            runnable = any(_due(job, naive_utc()) for job in session.scalars(select(Job).where(
                Job.status.in_(CLAIMABLE), Job.kind.in_(enabled_kinds()))))
        if not runnable:
            return
        asyncio.run(run_worker(once=True, stop=asyncio.Event()))
    pytest.fail("Your requested jobs did not drain through the local worker")


def assert_private_reply(reply):
    assert reply.get("kind") != "confirmation"
    assert not any(action.get("confirmation") for action in reply.get("actions", []))
    assert not re.search(r"\b\d+(?:[.,]\d+)?\s*credits?\b|\bcredits?\s*[:=]\s*\d",
                         reply.get("body_md", ""), re.I)


def assert_no_credentials(content, *credentials):
    assert not any(credential in content for credential in credentials), "Your credentials must remain private"


def chat(owner, message):
    response = owner.http.post(owner.path + "/chat", json={"message": message, "idempotency_key": secrets.token_hex(12)})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert_private_reply(reply)
    drain_jobs()
    return reply


def require_working_public_read(owner):
    chat(owner, f"Read {owner.boundary.source} and summarize its public facts.")
    assert any(path == "/source" for _, _, _, path in owner.boundary.requests), (
        "Your public page must be read before these tests can qualify its SSRF protection")
    owner.boundary.requests.clear()


@contextmanager
def visitor(application, **options):
    """Remove credentials auto-injected by tests/unit/conftest.py from visitor requests."""
    with TestClient(application, **options) as client:
        client.headers.pop("X-Local-Proxy-Secret", None)
        client.cookies.clear()
        yield client


@pytest.fixture
def observed_errors(owner, monkeypatch):
    """Observe failure locations without logging exception inputs or changing recovery."""
    from app.chat import recovery

    locations = []
    original = recovery.log_founder_exception

    def observe(error, venture_id, action):
        locations.append((type(error).__name__, [(frame.filename, frame.lineno, frame.name)
            for frame in traceback.extract_tb(error.__traceback__) if "/services/company_os/" in frame.filename]))
        return original(error, venture_id, action)

    monkeypatch.setattr(recovery, "log_founder_exception", observe)
    return locations


def test_owner_requested_journey_stays_private_and_preserves_mail_compliance(owner, acceptance, scenario, tmp_path, observed_errors):
    """You can request work without a confirmation card, sends, publication or hosted traffic."""
    from app.firstlook.service import ingest_builder_page

    class ProductClient:
        headers = {}

        def request(self, method, path, payload=None, *, html=False):
            drain_jobs()
            response = owner.http.request(method, path, json=payload, headers=self.headers)
            if response.status_code >= 400:
                raise acceptance.AcceptanceFailure("Check this route's next step and retry", status=response.status_code)
            if html:
                return response.text
            result = response.json()
            if path.endswith("/chat"):
                assert_private_reply(result)
            return result

    journey = acceptance.Journey(ProductClient(), SimpleNamespace(stub=True, idea=scenario[0], answer=scenario[1],
        output=tmp_path / "page.html", timeout=2))
    journey.venture = owner.venture["id"]
    journey.workspace = owner.venture["workspace_id"]
    assert owner.http.get(owner.path + "/onboarding").json()["status"] == "idle"
    try:
        product = journey.client
        created = product.request("POST", owner.path + "/idea/sessions", {})
        journey.session = created["id"]
        if product.request("GET", journey.session_path())["state"] != "reviewing":
            product.request("POST", journey.session_path("/messages"), {"text": scenario[1]})
            if product.request("GET", journey.session_path())["state"] != "reviewing":
                journey.skip()
        for step in ("confirm", "customers", "design", "invitations"):
            getattr(journey, step)()
        reply = product.request("POST", journey.path("/chat"), {
            "message": "Build a landing page for my saved idea. Keep it private and do not send anything",
            "idempotency_key": secrets.token_hex(16)})
        assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
        assert any(action.get("href") == "/settings/local#coding-tools" for action in reply["actions"])
        assert not journey.page() and not journey.args.output.exists()
        html = ('<!doctype html><html lang="en"><body><main><h1>' + escape(scenario[0])
                + '</h1><p>Sample page, not market evidence.</p></main></body></html>')
        assert ingest_builder_page(journey.venture, html)["ok"]
        journey.export()
    except acceptance.AcceptanceFailure as error:
        from app.db.models import Job
        from app.state import db
        from sqlalchemy import select

        with db()() as session:
            jobs = [(job.kind, job.status) for job in session.scalars(select(Job))]
        pytest.fail(f"{error}; saved jobs: {jobs}; failures: {observed_errors}; reply kind: {(journey.last_reply or {}).get('kind')}")
    assert journey.args.output.is_file()
    assert not owner.boundary.sent
    assert not any(method != "GET" and host in {"api.vercel.com", "api.netlify.com"}
                   for method, host, _, _ in owner.boundary.requests)


def test_file_backed_local_app_starts_before_you_use_its_owner_session(boundary, local_owner_app, scenario):
    """Your installed SQLite app must start without missing migration helper imports."""
    from core.local_security import _proxy_secret

    with TestClient(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        response = client.get("/auth/session", headers={"X-Local-Proxy-Secret": _proxy_secret().decode("ascii")})
        assert response.status_code == 200 and response.json()["authenticated"]


@pytest.mark.parametrize("method,suffix,payload", [
    ("GET", "/auth/session", None), ("GET", "/local/settings/search", None),
    ("POST", "/ventures", {"raw_idea": "Untrusted request", "start": True}),
    ("GET", "/ventures/missing/activity/stream", None), ("POST", "/internal/tick", {"tick": "daily"}),
    ("POST", "/internal/builds/missing/model-invocations", {}),
])
@pytest.mark.parametrize("proxy", [False, True], ids=["direct-api", "console-proxy"])
def test_every_private_route_requires_owner_before_work(owner, method, suffix, payload, proxy):
    """Loopback and a forwarded console host never replace your owner credential."""
    headers = {"X-Subject": "forged-owner", "X-Cos-Assertion": "forged-assertion", "Origin": "http://localhost:3100"}
    if proxy:
        headers["X-Forwarded-Host"] = "localhost:3100"
    with visitor(owner.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001)) as anonymous:
        response = anonymous.request(method, suffix.replace("{venture}", owner.path), json=payload, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "local_owner_required"
    assert "desktop app" in response.json()["detail"]["message"]
    assert not owner.boundary.sent and not owner.boundary.requests


@pytest.mark.parametrize("origin", ["http://localhost:3101", "https://localhost:3100", "http://127.0.0.1:3100",
    "http://localhost:3100/", "http://localhost:3100.attacker.example", "http://user@localhost:3100", "null"])
def test_exact_origin_rejects_other_ports_aliases_schemes_and_ambiguous_urls(owner, origin):
    """Even with your credential, a different browser origin cannot start work."""
    response = owner.http.post("/ventures", headers={"Origin": origin}, json={"raw_idea": "Untrusted origin", "start": True})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "local_origin_rejected"
    assert "Open your local app directly" in response.json()["detail"]["message"]
    assert not owner.boundary.requests and not owner.boundary.sent


@pytest.mark.parametrize("origin", ["http://localhost:3100", "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"])
def test_your_exact_console_and_desktop_origins_can_use_the_owner_session(owner, origin):
    """The actual desktop and console origins retain working local sign-in."""
    response = owner.http.get("/auth/session", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.json()["authenticated"] and response.json()["mode"] == "local"
    assert_no_credentials(response.text, owner.credential)


def test_your_authenticated_console_proxy_preserves_owner_and_origin_checks(owner):
    """The console proxy can reach its private backend only with your credential and exact browser origin."""
    with visitor(owner.app, base_url="http://company-os-core:3300", client=("172.20.0.2", 50001)) as proxy:
        headers = {"X-Local-Proxy-Secret": owner.credential, "X-Forwarded-Host": "localhost:3100",
                   "Origin": "http://localhost:3100"}
        response = proxy.get("/auth/session", headers=headers)
        assert response.status_code == 200 and response.json()["authenticated"]
        assert_no_credentials(response.text, owner.credential)
        assert proxy.get("/auth/session", headers=headers | {"Origin": "http://localhost:3101"}).status_code == 403
        assert proxy.get("/auth/session", headers={"X-Forwarded-Host": "localhost:3100"}).status_code == 403


@pytest.mark.parametrize("attack", ["duplicate-origin", "foreign-referer", "cross-site", "wrong-credential"])
def test_ambiguous_browser_headers_and_forged_credentials_fail_before_work(owner, attack):
    """Duplicate origins, another tab's origin and a forged owner credential cannot start work."""
    headers = {
        "duplicate-origin": [("Origin", "http://localhost:3100"), ("Origin", "https://attacker.example")],
        "foreign-referer": [("Referer", "http://localhost:3101/workspace")],
        "cross-site": [("Origin", "http://localhost:3100"), ("Sec-Fetch-Site", "cross-site")],
        "wrong-credential": [("X-Local-Proxy-Secret", "0" * 64), ("X-Subject", "forged-owner")],
    }[attack]
    response = owner.http.post("/ventures", headers=headers, json={"raw_idea": "Untrusted request", "start": True})
    assert response.status_code == 403
    assert not owner.boundary.requests and not owner.boundary.sent


def test_remote_override_does_not_grant_owner_or_disable_origin_checks(owner, monkeypatch):
    """Opening callback ingress must not make a remote visitor you."""
    monkeypatch.setenv("SUPYMEM_LOCAL_ALLOW_REMOTE", "1")
    monkeypatch.setenv("COMPANY_OS_PUBLIC_URL", "https://callbacks.example")
    with visitor(owner.app, base_url="https://callbacks.example", client=("198.51.100.42", 50001)) as remote:
        for path in ("/auth/session", "/local/settings/search", owner.path + "/activity/stream"):
            response = remote.get(path, headers={"X-Subject": "forged-owner"})
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "local_owner_required"
        response = remote.post("/ventures", headers={"X-Local-Proxy-Secret": owner.credential,
            "Origin": "https://attacker.example"}, json={"raw_idea": "Untrusted visitor", "start": True})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "local_origin_rejected"
    assert not owner.boundary.requests and not owner.boundary.sent


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/public/waitlists/missing", 404), ("POST", "/public/waitlists/missing", 422),
    ("GET", "/public/unsubscribe", 404), ("POST", "/public/unsubscribe", 422),
    ("GET", "/public/email/unsubscribe", 200), ("POST", "/public/email/unsubscribe", 200),
    ("GET", "/api/v1/ai-gtm/gmail/callback", 400),
    ("GET", "/api/v1/content/oauth/linkedin/callback", 400), ("POST", "/e", 204),
])
def test_public_callbacks_reach_their_own_validation_without_owner_access(owner, monkeypatch, method, path, status):
    """A public visitor reaches only signup, opt-out, event or OAuth validation."""
    monkeypatch.setenv("SUPYMEM_LOCAL_ALLOW_REMOTE", "1")
    monkeypatch.setenv("COMPANY_OS_PUBLIC_URL", "https://callbacks.example")
    with visitor(owner.app, base_url="https://callbacks.example", client=("198.51.100.42", 50001)) as remote:
        headers = {"Origin": "https://audience.example", "Sec-Fetch-Site": "cross-site", "X-Subject": "forged-owner"}
        response = remote.request(method, path, headers=headers, **({"json": {}} if method == "POST" else {}))
        assert response.status_code == status
        assert_no_credentials(response.text, owner.credential)
        assert remote.get("/local/settings/search", headers=headers).status_code == 403
    assert not owner.boundary.sent and not owner.boundary.requests


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/public/waitlists/missing/pricing/exp_" + "a" * 24 + "/test", 404),
    ("GET", "/public/waitlists/missing/surveys/exp_" + "a" * 24 + "/" + "b" * 20, 404),
    ("POST", "/public/waitlists/missing/surveys/exp_" + "a" * 24 + "/" + "b" * 20, 422),
])
def test_public_experiment_callbacks_reach_token_validation(owner, monkeypatch, method, path, status):
    """Public pricing and survey tokens protect callbacks without requiring your owner session."""
    monkeypatch.setenv("SUPYMEM_LOCAL_ALLOW_REMOTE", "1")
    monkeypatch.setenv("COMPANY_OS_PUBLIC_URL", "https://callbacks.example")
    with visitor(owner.app, base_url="https://callbacks.example", client=("198.51.100.42", 50001)) as remote:
        response = remote.request(method, path, json={})
        assert response.status_code == status
        assert remote.get("/auth/session").status_code == 403
    assert not owner.boundary.requests and not owner.boundary.sent


@pytest.mark.parametrize("method,path", [("POST", "/api/v1/ai-gtm/gmail/callback"),
    ("POST", "/public/waitlists/missing/pricing/exp_" + "a" * 24 + "/test"),
    ("GET", "/public/waitlists/missing/pricing/missing/test"),
    ("GET", "/public/waitlists/missing/surveys/exp_" + "a" * 24 + "/short"),
    ("GET", "/public/waitlists/missing/settings"), ("POST", "/api/v1/content/oauth/linkedin/callback"),
    ("GET", "/e"), ("GET", "/api/v1/content/oauth/linkedin/status")])
def test_callback_allowlist_is_exact_in_path_and_method(owner, method, path):
    """Callback-like paths and wrong methods cannot enter your private API."""
    with visitor(owner.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001)) as anonymous:
        response = anonymous.request(method, path, json={})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "local_owner_required"


@pytest.mark.parametrize("platform", ["googlesheets", "google_calendar"])
def test_shipped_connection_callback_validates_one_use_state_without_owner(owner, monkeypatch, platform):
    from app.content.oauth import attempts, callbacks
    from app.core import local_owner
    from app.tenancy import bind
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from app.authz.http import independent_auth
    from starlette.requests import Request

    workspace_id = owner.venture["workspace_id"]
    path = f"/api/v1/content/oauth/{platform}/callback"
    request = Request({"type": "http", "method": "GET", "path": path, "headers": []})
    assert independent_auth(request, "app.api.ops_api")
    assert not independent_auth(request, "app.api.connections_api")
    with monkeypatch.context() as default_mode:
        default_mode.delenv("SUPYMEM_MODE", raising=False)
        assert independent_auth(request, "app.api.ops_api")
        private = Request({"type": "http", "method": "GET", "path": path.replace("/callback", "/start"), "headers": []})
        assert not independent_auth(private, "app.api.ops_api")
    with bind(workspace_id=workspace_id, venture_id=owner.venture["id"], actor=LOCAL_PERSON_ID):
        state = attempts.create(workspace_id=workspace_id, platform=platform,
            redirect_uri="http://127.0.0.1" + path,
            return_to=f"/connections?workspace={workspace_id}&venture={owner.venture['id']}", auth_method="composio")
        attempts.bind_connection(state, user_id="owned-user", auth_config_id="owned-config",
            connection_id="owned-connection")
    completed = []
    monkeypatch.setattr(callbacks, "_complete_managed", lambda attempt: completed.append(attempt))
    with visitor(owner.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001)) as anonymous:
        monkeypatch.setattr(local_owner, "ensure_owner", lambda: pytest.fail("Callbacks must not acquire owner identity"))
        headers = {"X-Subject": "forged-owner", "X-COS-Assertion": "forged"}
        invalid = anonymous.get(path, params={"supymem_state": secrets.token_urlsafe(32)}, headers=headers)
        assert invalid.status_code == 400 and invalid.json()["detail"]["code"] == "invalid_state"
        wrong_platform = "google_calendar" if platform == "googlesheets" else "googlesheets"
        wrong = anonymous.get(f"/api/v1/content/oauth/{wrong_platform}/callback",
            params={"supymem_state": state}, headers=headers)
        assert wrong.status_code == 400 and wrong.json()["detail"]["code"] == "invalid_state"
        valid = anonymous.get(path, params={"supymem_state": state}, headers=headers, follow_redirects=False)
        assert valid.status_code == 200 and "oauth=connected" in valid.text
        consumed = anonymous.get(path, params={"supymem_state": state}, headers=headers)
        assert consumed.status_code == 400 and consumed.json()["detail"]["code"] == "invalid_state"
        assert anonymous.get("/auth/session", headers=headers).status_code == 403
    assert len(completed) == 1 and completed[0]["workspace_id"] == workspace_id
    assert not owner.boundary.requests and not owner.boundary.sent


@pytest.mark.parametrize("outcome", ["published", "race", "failed"])
@pytest.mark.parametrize("platform", ["posix", "nt"], ids=["unix", "windows-contract"])
def test_owner_session_closes_before_create_only_publication_and_cleanup(monkeypatch, tmp_path, scenario, outcome, platform):
    from app.core import local_owner
    from app.core import local_windows
    from core.local_security import _proxy_secret, owner_secret_path

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / scenario[2]))
    monkeypatch.delenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", raising=False)
    synced = []
    fsync, link, unlink = os.fsync, os.link, os.unlink
    winner = secrets.token_hex(32).encode()

    def sync(descriptor):
        fsync(descriptor)
        synced.append(descriptor)

    def publish(source, destination):
        with pytest.raises(OSError):
            os.fstat(synced[-1])
        if outcome == "failed":
            raise PermissionError("publication denied")
        if outcome == "race":
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(winner)
        link(source, destination)

    def cleanup(path, *args, **kwargs):
        with pytest.raises(OSError):
            os.fstat(synced[-1])
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "link", publish)
    monkeypatch.setattr(os, "unlink", cleanup)
    private_paths = []
    if platform == "nt":
        def private_directory(path):
            path.mkdir(parents=True, exist_ok=True)

        def private_open(path, flags):
            private_paths.append(path)
            return os.open(path, flags, 0o600)

        monkeypatch.setattr("app.config.data_root", lambda: None)
        monkeypatch.setattr(local_windows, "private_directory", private_directory)
        monkeypatch.setattr(local_windows, "private_open", private_open)
        monkeypatch.setattr(local_owner, "os", SimpleNamespace(name="nt", O_WRONLY=os.O_WRONLY,
            O_CREAT=os.O_CREAT, O_EXCL=os.O_EXCL, fdopen=os.fdopen, fsync=sync, link=publish, unlink=cleanup))
    if outcome == "failed":
        with pytest.raises(PermissionError, match="publication denied"):
            local_owner._ensure_proxy_secret()
        assert not owner_secret_path().exists()
    else:
        local_owner._ensure_proxy_secret()
        first = _proxy_secret()
        if outcome == "race":
            assert secrets.compare_digest(first, winner)
        local_owner._ensure_proxy_secret()
        assert secrets.compare_digest(first, _proxy_secret())
    assert not list(owner_secret_path().parent.glob("*.tmp"))
    if platform == "nt":
        assert len(private_paths) == 1


@pytest.mark.parametrize("rejected", [False, True], ids=["private", "unsafe-acl-or-reparse"])
def test_windows_owner_session_reader_uses_validated_private_handle(monkeypatch, tmp_path, scenario, rejected):
    from app.core import local_windows
    from core import local_security
    from fastapi import HTTPException

    path = tmp_path / scenario[2]
    path.write_bytes(secrets.token_hex(32).encode())
    monkeypatch.setattr(local_security, "owner_secret_path", lambda: path)
    calls = []

    def private_open(filename, flags):
        calls.append((filename, flags))
        if rejected:
            raise ValueError("unsafe owner, ACL or reparse point")
        return os.open(filename, flags)

    monkeypatch.setattr(local_windows, "private_open", private_open)
    monkeypatch.setattr(local_security, "os", SimpleNamespace(name="nt", O_RDONLY=os.O_RDONLY,
        O_NONBLOCK=os.O_NONBLOCK, open=os.open, fdopen=os.fdopen, fstat=os.fstat))
    if rejected:
        with pytest.raises(HTTPException) as failure:
            local_security._proxy_secret()
        assert failure.value.status_code == 503
        assert "data-folder permissions" in failure.value.detail["message"]
    else:
        assert len(local_security._proxy_secret()) == 64
    assert calls == [(path, os.O_RDONLY)]


def test_sse_accepts_your_cookie_but_not_anonymous_or_forged_subject(owner):
    """Your activity stream uses the owner cookie, not an attacker-supplied subject."""
    from core.local_security import OWNER_COOKIE

    with visitor(owner.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001)) as browser:
        path = "/ventures/missing/activity/stream"
        assert browser.get(path, headers={"X-Subject": "forged-owner"}).status_code == 403
        browser.cookies.set(OWNER_COOKIE, owner.credential)
        response = browser.get(path, headers={"Origin": "http://localhost:3100"})
        assert response.status_code == 404
        assert_no_credentials(response.text, owner.credential)
        assert browser.get(path, headers={"Origin": "https://attacker.example"}).status_code == 403

    async def read_first_event():
        finished = asyncio.Event()
        responses = []
        body_received = False

        async def receive():
            nonlocal body_received
            if not body_received:
                body_received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await finished.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            responses.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                finished.set()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"}, "method": "GET",
            "scheme": "http", "path": owner.path + "/activity/stream", "root_path": "", "query_string": b"",
            "http_version": "1.1", "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 50001),
            "headers": [(b"host", b"127.0.0.1"), (b"origin", b"http://localhost:3100"),
                        (b"cookie", f"{OWNER_COOKIE}={owner.credential}".encode())]}
        await asyncio.wait_for(owner.app(scope, receive, send), timeout=5)
        return responses

    events = asyncio.run(read_first_event())
    assert next(item for item in events if item["type"] == "http.response.start")["status"] == 200
    body = b"".join(item.get("body", b"") for item in events)
    assert b"event: activity" in body or b": heartbeat" in body
    assert_no_credentials(body.decode(), owner.credential)


@pytest.mark.parametrize("section,payload", [
    ("search", {"provider": "tavily"}),
    ("smtp", {"host": "smtp.example", "port": 587, "user": "owner@example.com", "sender_name": "Your team",
              "postal_address": "100 Test Street, Test City", "imap_host": "imap.example"}),
    ("publishing", {"provider": "vercel", "project": "test-project"}),
    ("composio", {"auth_configs": {"gmail": "test-auth-config"}}),
])
def test_saved_keys_and_connection_errors_never_return_log_or_store_plaintext_secrets(owner, tmp_path, caplog, section, payload):
    """You save a key once; connection checks expose only status and a precise recovery step."""
    caplog.set_level("DEBUG")
    secret = secrets.token_hex(32)
    owner.boundary.secret = secret
    field = {"search": "api_key", "smtp": "password", "publishing": "token", "composio": "api_key"}[section]
    responses = [owner.http.put("/local/settings/" + section, json=payload | {field: secret})]
    assert responses[0].status_code == 200
    responses.append(owner.http.get("/local/settings/" + section))
    assert responses[-1].json()["secrets_set"][field]
    responses.extend([owner.http.get("/local/settings/catalog"), owner.http.get("/setup/status"),
                      owner.http.get("/auth/session")])
    owner.boundary.failure = True
    responses.append(owner.http.post("/local/settings/" + section + "/test", json={}))
    error = responses[-1]
    assert error.status_code in {200, 409, 422, 503}
    assert "Keys and connections" in error.text
    assert all(secret not in response.text and owner.credential not in response.text for response in responses)
    assert_no_credentials(caplog.text, secret, owner.credential)
    settings_files = list(tmp_path.glob("local-settings*"))
    assert settings_files
    assert all(secret.encode() not in path.read_bytes() for path in settings_files if path.is_file())
    assert not owner.boundary.sent


@pytest.mark.parametrize("name", ["research", "find_customers", "send_email", "read_replies", "landing_pages",
    "publish_pages", "page_measurement", "background_work", "run_for_me_experiments", "routines", "connected_apps", "content"])
def test_each_feature_can_be_switched_off_in_your_app_with_the_exact_next_step(owner, name):
    """Every shipped local feature has an owner switch, not an environment/admin gate."""
    response = owner.http.put("/local/capabilities/" + name, json={"on": False})
    assert response.status_code == 200
    feature = response.json()
    assert feature["state"] == "off"
    assert "Settings > Capabilities" in feature["next_step"] and feature["title"] in feature["next_step"]
    catalog = owner.http.get("/local/capabilities").json()["capabilities"]
    assert next(item for item in catalog if item["name"] == name)["state"] == "off"
    assert not owner.boundary.sent and not owner.boundary.requests


@pytest.mark.parametrize("name,section,payload,missing,screen", [
    ("research", "search", {"provider": "tavily"}, "Search API key", "Your search and page reader"),
    ("send_email", "smtp", {}, "Mail app password", "Your mail account"),
    ("read_replies", "smtp", {}, "Incoming mail host", "Your mail account"),
    ("publish_pages", "publishing", {"provider": "vercel", "project": "test-project"},
     "Publishing token", "Your page publishing account"),
    ("connected_apps", "composio", {}, "Composio project key", "Your connected apps"),
])
def test_missing_settings_name_the_field_and_screen_you_can_fix(owner, name, section, payload, missing, screen):
    """Setup gaps tell you which saved setting to change and where."""
    assert owner.http.put("/local/settings/" + section, json=payload).status_code == 200
    response = owner.http.put("/local/capabilities/" + name, json={"on": True})
    assert response.status_code == 200
    feature = response.json()
    assert feature["state"] == "needs_setup" and missing in feature["missing"]
    assert missing in feature["next_step"] and screen in feature["next_step"]
    assert "Settings > Keys and connections" in feature["next_step"]
    assert not owner.boundary.sent and not owner.boundary.requests


@pytest.mark.parametrize("state", ["off", "needs_setup"])
def test_requested_research_names_the_exact_setup_step_and_does_not_run_when_blocked(owner, state):
    """A requested page read respects your switch and tells you the precise recovery step."""
    if state == "needs_setup":
        assert owner.http.put("/local/settings/search", json={"provider": "tavily"}).status_code == 200
    feature = owner.http.put("/local/capabilities/research", json={"on": state != "off"}).json()
    reply = chat(owner, f"Read {owner.boundary.source} and summarize its public facts.")
    assert feature["next_step"] in reply["body_md"]
    assert not owner.boundary.requests and not owner.boundary.sent


@pytest.mark.parametrize("state", ["off", "needs_setup"])
def test_a_blocked_local_reader_never_falls_back_to_another_reader(owner, state):
    """Switching research off or removing its key prevents reads, including legacy fallbacks."""
    if state == "needs_setup":
        assert owner.http.put("/local/settings/search", json={"provider": "tavily"}).status_code == 200
    assert owner.http.put("/local/capabilities/research", json={"on": state != "off"}).status_code == 200
    response = owner.http.post(owner.path + "/chat", json={"message": f"Read {owner.boundary.source} and summarize it.",
        "idempotency_key": secrets.token_hex(12)})
    assert response.status_code == 200
    assert not owner.boundary.requests and not owner.boundary.sent


@pytest.mark.parametrize("state", ["off", "needs_setup"])
def test_requested_publication_is_blocked_before_network_and_names_your_setting(owner, state, scenario):
    """A publish request without your switch or token cannot contact a publishing service."""
    sample = owner.http.post(owner.path + "/site/local/publish", json={})
    assert sample.status_code == 409 and "Your sample page stays private" in sample.text
    assert owner.http.put("/local/providers", json={"provider": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1", "models": {"chat": "offline-fixture"}}).status_code == 200
    real = owner.http.post("/ventures", json={"raw_idea": scenario[0], "start": False})
    assert real.status_code == 201
    venture = real.json()
    from app.core.local_mode import is_sample_workspace

    assert not is_sample_workspace(venture["workspace_id"])
    owner.http.headers["X-Workspace-Id"] = venture["workspace_id"]
    assert owner.http.put("/local/settings/publishing", json={"provider": "vercel", "project": "test-project"}).status_code == 200
    feature = owner.http.put("/local/capabilities/publish_pages", json={"on": state != "off"}).json()
    response = owner.http.post(f"/ventures/{venture['id']}/site/local/publish", json={})
    assert response.status_code == 409
    assert feature["next_step"] in response.text
    assert not owner.boundary.requests and not owner.boundary.sent


@pytest.mark.parametrize("section,host", [("smtp", "smtp.example"), ("imap", "imap.example")])
def test_testing_your_saved_mail_connection_only_signs_in_and_never_sends(owner, section, host):
    """Saving and testing your own outgoing/incoming mail password is not permission to send."""
    password = secrets.token_hex(32)
    saved = owner.http.put("/local/settings/smtp", json={"host": "smtp.example", "port": 587,
        "user": "owner@example.com", "password": password, "sender_name": "Your team",
        "postal_address": "100 Test Street, Test City", "imap_host": "imap.example", "imap_port": 993})
    assert saved.status_code == 200
    response = owner.http.post("/local/settings/" + section + "/test", json={})
    assert response.status_code == 200 and response.json()["ok"]
    assert owner.boundary.logins == [(host, "owner@example.com")]
    assert_no_credentials(saved.text + response.text, password)
    assert not owner.boundary.sent and not owner.boundary.requests


def test_an_owner_suppressed_contact_is_ineligible_and_its_address_is_not_exposed(owner):
    """Your suppression survives owner-authenticated requests and prevents further outreach."""
    email = f"contact-{secrets.token_hex(6)}@example.invalid"
    preview = owner.http.post(owner.path + "/gtm/contacts/import/preview", json={
        "text": f"email,country\n{email},US", "format": "csv"})
    assert preview.status_code == 200 and preview.json()["valid_rows"] == 1
    imported = owner.http.post(owner.path + "/gtm/contacts/import", json={
        "preview_id": preview.json()["preview_id"], "lawful_basis_attested": True, "lawful_basis": "consent"})
    assert imported.status_code == 200
    contact_id = imported.json()["contact_ids"][0]
    suppressed = owner.http.patch("/api/v1/ai-gtm/contacts/" + contact_id, json={"do_not_contact": True})
    assert suppressed.status_code == 200
    response = owner.http.get("/api/v1" + owner.path + "/gtm/journey")
    assert response.status_code == 200
    contact = next(item for item in response.json()["prospects"] if item["id"] == contact_id)
    assert contact["blocked_reason"] == "suppressed_or_replied"
    assert not contact["eligible"] and contact["email"] is None
    assert email not in json.dumps(contact)
    assert not owner.boundary.sent


@pytest.mark.parametrize("cap,draft_count", [(6, 1), (1, 6), (0, 1)])
def test_an_owner_request_cannot_bypass_the_send_batch_cap(owner, cap, draft_count):
    """Your owner credential does not bypass the mandatory send cap at the HTTP boundary."""
    response = owner.http.post("/api/v1" + owner.path + "/gtm/journey/send", json={
        "batch_id": "test-batch", "draft_ids": [f"draft-{index}" for index in range(draft_count)],
        "approval_digest": "0" * 64, "cap": cap})
    assert response.status_code == 422
    drain_jobs()
    assert not owner.boundary.requests and not owner.boundary.sent


def test_builtin_reader_works_with_saved_defaults_and_never_sends_or_publishes(owner, observed_errors):
    """Your default public-page reader needs neither Firecrawl nor a paid search key."""
    assert owner.http.put("/local/settings/search", json={"provider": "builtin"}).status_code == 200
    assert owner.http.put("/local/capabilities/research", json={"on": True}).json()["state"] == "ready"
    chat(owner, f"Read {owner.boundary.source} and summarize its public facts.")
    assert any(path == "/source" for _, _, _, path in owner.boundary.requests), observed_errors
    assert all(method == "GET" for method, _, _, _ in owner.boundary.requests)
    assert not owner.boundary.sent


@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "http://[::1]/private", "http://169.254.169.254/latest/meta-data",
    "http://10.1.2.3/private", "http://[::ffff:127.0.0.1]/private", "https://api.supymem.com/private",
    "https://supymem.com/private", "https://user:password@public.example/private"])
def test_pasted_unsafe_urls_never_reach_external_transport(owner, target):
    """Reading a pasted link cannot reach loopback, metadata, private IPs or Supymem."""
    require_working_public_read(owner)
    chat(owner, f"Summarize the public facts from {target}. Do not send or publish anything.")
    assert not owner.boundary.requests and not owner.boundary.sent


@pytest.mark.parametrize("attack", ["private-answer", "mixed-dns", "private-redirect", "hosted-redirect"])
def test_builtin_reader_checks_all_dns_answers_and_each_redirect(owner, scenario, attack):
    """Public names cannot redirect or resolve into private services or hosted Supymem."""
    require_working_public_read(owner)
    if attack == "private-answer":
        owner.boundary.addresses[scenario[2]] = ["10.1.2.3"]
    elif attack == "mixed-dns":
        owner.boundary.addresses[scenario[2]] = ["93.184.216.34", "10.1.2.3"]
    else:
        owner.boundary.redirect = ("http://169.254.169.254/latest/meta-data" if attack == "private-redirect"
                                   else "https://api.supymem.com/private")
    chat(owner, f"Read {owner.boundary.source} and summarize its public facts.")
    if attack in {"private-answer", "mixed-dns"}:
        assert not owner.boundary.requests
    else:
        assert any(path == "/source" for _, _, _, path in owner.boundary.requests)
        assert all(path in {"/robots.txt", "/source"} for _, _, _, path in owner.boundary.requests)
    assert not owner.boundary.sent
