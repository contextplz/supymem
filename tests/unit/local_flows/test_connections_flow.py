"""Owner journeys for local connections, with only provider transports replaced."""

import asyncio
import json
import re
import secrets
import socket
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import NullPool

from tests.unit.test_local_mode import _owner_client, local_owner_app


@pytest.fixture(params=[
    pytest.param("A maintenance planner for commercial refrigeration contractors", id="b2b"),
    pytest.param("A stargazing journal for amateur astronomers", id="consumer"),
])
def idea(request):
    return request.param


class ProviderTransport:
    def __init__(self):
        self.requests = []
        self.accounts = {}
        self.sent = []
        self.smtp_connections = []
        self.imap_connections = []
        self.unexpected = []
        self.secret = secrets.token_hex(24)
        self.password = secrets.token_hex(24)
        self.provider_token = secrets.token_hex(24)
        self.error_status = None

    def respond(self, request):
        host, path = request.url.host, request.url.path
        self.requests.append(request)
        assert host != "supymem.com" and not host.endswith(".supymem.com"), "Local work contacted a hosted Supymem service"
        if host not in {"backend.composio.dev", "backend.composio.io"}:
            self.unexpected.append((request.method, host, path))
            return httpx.Response(503, json={"message": "Unexpected external request"})
        saved_key_used = secrets.compare_digest(request.headers.get("x-api-key", ""), self.secret)
        assert saved_key_used, "Provider did not use your saved Composio key"
        if self.error_status:
            return httpx.Response(self.error_status, json={"error": {"message": self.provider_token}})
        body = json.loads(request.content) if request.content else {}
        if path.endswith("/connected_accounts/link") and request.method == "POST":
            account_id = "ca_" + secrets.token_hex(8)
            self.accounts[account_id] = {
                "id": account_id, "user_id": body["user_id"], "auth_config": {"id": body["auth_config_id"]},
                "toolkit": {"slug": body["auth_config_id"].removeprefix("ac_")}, "status": "ACTIVE",
                "access_token": self.provider_token,
            }
            return httpx.Response(200, json={"id": account_id, "connected_account_id": account_id,
                "redirect_url": "https://connect.composio.dev/authorize/" + account_id})
        account_id = path.rsplit("/", 1)[-1]
        if "/connected_accounts/" in path and account_id in self.accounts:
            return httpx.Response(200, json=self.accounts[account_id])
        if path.endswith("/connected_accounts"):
            return httpx.Response(200, json={"items": list(self.accounts.values())})
        if path.endswith("/tools"):
            return httpx.Response(200, json={"items": [{"slug": slug} for slug in (
                "GOOGLESHEETS_CREATE_GOOGLE_SHEET", "GOOGLESHEETS_BATCH_UPDATE_SPREADSHEET")]})
        if "/tools/execute/" in path:
            if account_id == "GOOGLESHEETS_CREATE_GOOGLE_SHEET":
                data = {"spreadsheetId": "owner_results", "sheets": [{"properties": {"sheetId": 0, "title": "Results"}}]}
            elif account_id == "GOOGLESHEETS_BATCH_UPDATE_SPREADSHEET":
                data = {"updatedRows": len(body["arguments"]["values"])}
            else:
                self.unexpected.append((request.method, host, path))
                return httpx.Response(503, json={"successful": False})
            return httpx.Response(200, json={"successful": True, "data": data})
        if "/tools/" in path:
            fields = {"title": {"type": "string"}} if account_id == "GOOGLESHEETS_CREATE_GOOGLE_SHEET" else {
                "spreadsheet_id": {"type": "string"}, "values": {"type": "array"}, "value_input_option": {"type": "string"}}
            return httpx.Response(200, json={"input_parameters": {"type": "object", "properties": fields,
                "required": list(fields)}})
        self.unexpected.append((request.method, host, path))
        return httpx.Response(503, json={"message": "Unexpected Composio request"})

    def writes(self):
        return [request for request in self.requests if "/tools/execute/" in request.url.path]


def require_local_send_pricing(response):
    assert response.json().get("detail", {}).get("code") != "outbound_rate_configuration_missing"


@pytest.fixture
def flow(local_owner_app, monkeypatch, idea, tmp_path):
    from app import state
    from app.db.session import _register_sqlite_pragmas
    from app.core.local_mode import is_local

    assert is_local()
    database_url = f"sqlite:///{tmp_path}/connections.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", database_url)
    engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30}, poolclass=NullPool)
    _register_sqlite_pragmas(engine)
    monkeypatch.setattr(state, "make_engine", lambda url: engine)
    transport = ProviderTransport()
    original_client, original_async_client = httpx.Client, httpx.AsyncClient

    def external_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport.respond)
        return original_client(*args, **kwargs)

    def external_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport.respond)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", external_client)
    monkeypatch.setattr(httpx, "AsyncClient", external_async_client)
    monkeypatch.setenv("CONNECT_POLL_SECONDS", "0")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("GUESSED_EMAILS", "off")

    def no_network(*args, **kwargs):
        raise OSError("External network is disabled in this offline flow")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)

    class SMTP:
        def __init__(self, host, port, **kwargs):
            transport.smtp_connections.append((host, port))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            return 250, b"ready"

        def starttls(self, **kwargs):
            return 220, b"ready"

        def login(self, user, password):
            saved_mailbox_used = user == "owner@mail.example" and secrets.compare_digest(password, transport.password)
            assert saved_mailbox_used, "SMTP did not use your saved mailbox"
            return 235, b"ready"

        def send_message(self, message, **kwargs):
            transport.sent.append(message)
            return {}

    class IMAP:
        def __init__(self, host, port, **kwargs):
            transport.imap_connections.append((host, port))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            saved_mailbox_used = user == "owner@mail.example" and secrets.compare_digest(password, transport.password)
            assert saved_mailbox_used, "IMAP did not use your saved mailbox"
            return "OK", []

        def select(self, mailbox, readonly=False):
            assert readonly, "Connection checks must not modify your inbox"
            return "OK", [b"0"]

        def response(self, name):
            return name, [b"1"]

        def uid(self, command, *args):
            assert command == "search", "An empty inbox should only be searched"
            return "OK", [b""]

    monkeypatch.setattr("smtplib.SMTP", SMTP)
    monkeypatch.setattr("smtplib.SMTP_SSL", SMTP)
    monkeypatch.setattr("imaplib.IMAP4_SSL", IMAP)
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.post("/local/providers/sample", json={}).status_code == 200
        created = client.post("/ventures", json={"raw_idea": idea, "start": False})
        assert created.status_code == 201, created.text
        venture = created.json()
        client.headers["X-Workspace-Id"] = venture["workspace_id"]
        yield SimpleNamespace(client=client, venture=venture, transport=transport, idea=idea,
            chat_path=f"/ventures/{venture['id']}/chat", journey_path=f"/api/v1/ventures/{venture['id']}/gtm/journey")
        assert not any(request.url.host == "supymem.com" or request.url.host.endswith(".supymem.com")
            for request in transport.requests), "Your local flow attempted to contact hosted Supymem"
    engine.dispose()


def drain(expected_kinds=()):
    """Lease and execute queued work using the actual embedded local worker loop."""
    from app.db.models import Job
    from app.state import db
    from app.worker.main import _run_worker

    async def run():
        for attempt in range(12):
            with db()() as session:
                active = list(session.scalars(select(Job).where(Job.status.in_(["pending", "leased", "running", "retry_wait"]))))
            assert {job.kind for job in active} <= set(expected_kinds), "Your connection request unexpectedly queued unrelated work"
            await _run_worker(once=True, stop=asyncio.Event())
            with db()() as session:
                active = list(session.scalars(select(Job).where(Job.status.in_(["pending", "leased", "running", "retry_wait"]))))
            if not active:
                return
        pytest.fail("Your requested local jobs did not finish")

    asyncio.run(run())


def save_apps(flow, configs=None, *, key=True, on=True):
    body = {"auth_configs": configs or {}}
    if key:
        body["api_key"] = flow.transport.secret
    assert flow.client.put("/local/settings/composio", json=body).status_code == 200
    response = flow.client.put("/local/capabilities/connected_apps", json={"on": on})
    assert response.status_code == 200
    return response.json()


def save_mail(flow, *, send=True, read=True):
    response = flow.client.put("/local/settings/smtp", json={"host": "smtp.mail.example", "port": 587,
        "user": "owner@mail.example", "password": flow.transport.password, "sender_name": "Your research team",
        "postal_address": "42 Observatory Road, Portland, OR 97201", "imap_host": "imap.mail.example", "imap_port": 993})
    assert response.status_code == 200
    for name, on in (("send_email", send), ("read_replies", read)):
        assert flow.client.put("/local/capabilities/" + name, json={"on": on}).status_code == 200


def connect(flow, platform, toolkit):
    save_apps(flow, {toolkit: "ac_" + toolkit})
    result = flow.client.post("/api/v1/connections/" + platform + "/connect", json={})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["ok"] and body.get("authorize_url"), body.get("message")
    state = body.get("state")
    if not state:
        request = next(request for request in reversed(flow.transport.requests) if request.method == "POST")
        callback = json.loads(request.content).get("callback_url") or json.loads(request.content).get("redirect_url")
        state = parse_qs(urlsplit(callback).query)["supymem_state"][0]
    callback = flow.client.get("/api/v1/content/oauth/" + platform + "/callback",
        params={"supymem_state": state}, follow_redirects=False)
    assert callback.status_code == 200 and "oauth=connected" in callback.text
    synced = flow.client.post("/api/v1/connections/" + platform + "/sync")
    assert synced.status_code == 200 and synced.json()["ok"]
    drain()


def founder_reply(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("kind") != "confirmation" and not body.get("confirmation"), "You should not confirm work you requested"
    assert not any(action.get("confirmation") for action in body.get("actions", [])), "Your requested work produced a confirmation card"
    text = body.get("body_md", body.get("message", ""))
    assert not re.search(r"\b\d+\s+credits?\b|\bcredits?\s*[:=]\s*\d+", text, re.I), "Your reply displayed a credit count"
    assert "\u2014" not in text
    return body


def test_local_authorization_returns_a_completion_document_and_consumes_state(flow):
    domain = "coolant-ledgers.test" if "contractors" in flow.venture["raw_idea"] else "lunar-journals.test"
    save_apps(flow, {"googlesheets": "ac_googlesheets"})
    started = flow.client.post("/api/v1/connections/googlesheets/connect", json={})
    assert started.status_code == 200 and started.json()["ok"]
    request = next(request for request in reversed(flow.transport.requests) if request.method == "POST")
    callback = urlsplit(json.loads(request.content)["callback_url"])
    local_path = f"{callback.path}?{callback.query}"
    headers = {"X-Local-Proxy-Secret": "", "Referer": f"https://{domain}/authorize", "Sec-Fetch-Site": "cross-site"}
    completed = flow.client.get(local_path, headers=headers, follow_redirects=False)
    assert completed.status_code == 200
    assert completed.headers["cache-control"] == "no-store"
    assert completed.headers["referrer-policy"] == "no-referrer"
    assert completed.headers["content-type"].startswith("text/html")
    assert f'href="{callback.scheme}://{callback.netloc}/connections?provider=googlesheets&amp;oauth=connected"' in completed.text
    assert callback.query not in completed.text
    assert flow.client.get("/api/v1/connections/status").json()["connections"]["googlesheets"]["connected"]
    reused = flow.client.get(local_path, headers=headers, follow_redirects=False)
    assert reused.status_code == 400 and "Connections" in reused.text


@pytest.mark.parametrize("platform,toolkit", [("googlesheets", "googlesheets"), ("google_calendar", "googlecalendar")])
def test_owner_connects_with_saved_key_and_authorization_without_starting_work(flow, platform, toolkit, caplog):
    """Saving your own key and authorizing an app makes it ready, not permission to send or publish."""
    connect(flow, platform, toolkit)
    status = flow.client.get("/api/v1/connections/status")
    assert status.status_code == 200
    card = status.json()["connections"][platform]
    assert card["connected"] and card["ready"]
    reply = founder_reply(flow.client.post(flow.chat_path, json={"message": "Show my connections"}))
    assert "Connected" in reply["body_md"]
    for response in (status, flow.client.get("/local/settings/composio"), flow.client.get(flow.chat_path)):
        assert not any(secret in response.text for secret in (flow.transport.secret, flow.transport.provider_token)), "An API returned a secret"
    assert not any(secret in caplog.text for secret in (flow.transport.secret, flow.transport.provider_token)), "Logs exposed a secret"
    assert not flow.transport.writes() and not flow.transport.sent
    assert not flow.transport.smtp_connections and not flow.transport.imap_connections


@pytest.mark.parametrize("blocked", ["off", "key", "authorization"])
def test_connections_page_keeps_unconfigured_shipped_apps_visible(flow, blocked):
    """Connections keeps your shipped apps visible so you can open their exact setup step."""
    save_apps(flow, {} if blocked == "authorization" else {"googlesheets": "ac_googlesheets"},
        key=blocked != "key", on=blocked != "off")
    status = flow.client.get("/api/v1/connections/status").json()["connections"].get("googlesheets")
    assert status is not None, "Connections hides your shipped app instead of showing its setup step"
    assert not status["ready"] and not status["connectable"]


@pytest.mark.parametrize("blocked", ["off", "key", "authorization"])
def test_blocked_connections_name_the_exact_setting_and_screen_without_side_effects(flow, blocked):
    """A disabled switch or missing key/authorization tells you exactly what to save or enable."""
    readiness = save_apps(flow, {} if blocked == "authorization" else {"googlesheets": "ac_googlesheets"},
        key=blocked != "key", on=blocked != "off")
    for response in (flow.client.post("/api/v1/connections/googlesheets/connect", json={}),
            flow.client.post("/api/v1/connections/googlesheets/sync")):
        reply = founder_reply(response)
        text = reply.get("body_md", reply.get("message", ""))
        if blocked == "authorization":
            assert "Google Sheets authorization ID" in text and "App authorization IDs" in text
            assert "Keys and connections" in text
        else:
            assert readiness["next_step"] in text
            assert ("Settings > Capabilities" in text and "Connected apps" in text) if blocked == "off" else ("Keys and connections" in text and "Composio project key" in text)
    drain()
    assert not flow.transport.requests and not flow.transport.sent


@pytest.mark.parametrize("blocked", ["off", "key", "authorization"])
def test_blocked_toolkit_chat_returns_setup_instead_of_researching_or_starting_work(flow, blocked):
    """Asking to connect a shipped app still names its setup step when that app is not configured."""
    readiness = save_apps(flow, {} if blocked == "authorization" else {"googlesheets": "ac_googlesheets"},
        key=blocked != "key", on=blocked != "off")
    reply = founder_reply(flow.client.post(flow.chat_path, json={"message": "Connect my Google Sheets account"}))
    assert not flow.transport.requests and not flow.transport.sent, "Connecting a blocked app must not start web research"
    if blocked == "authorization":
        assert "Google Sheets authorization ID" in reply["body_md"] and "App authorization IDs" in reply["body_md"]
        assert "Keys and connections" in reply["body_md"]
    else:
        assert readiness["next_step"] in reply["body_md"]
    drain()


def test_requested_sheet_export_routes_non_mail_tools_to_your_composio_account(flow):
    """Your requested export creates and fills a new sheet without SMTP or another confirmation."""
    connect(flow, "googlesheets", "googlesheets")
    reply = founder_reply(flow.client.post(flow.chat_path, json={"message": "Export my experiment results to Google Sheets"}))
    drain()
    tools = [request.url.path.rsplit("/", 1)[-1] for request in flow.transport.writes()]
    assert tools == ["GOOGLESHEETS_CREATE_GOOGLE_SHEET", "GOOGLESHEETS_BATCH_UPDATE_SPREADSHEET"], reply["body_md"]
    assert "https://docs.google.com/spreadsheets/d/owner_results/edit" in reply["body_md"]
    for request in flow.transport.writes():
        body = json.loads(request.content)
        assert body["connected_account_id"] in flow.transport.accounts
        assert body["user_id"] == flow.transport.accounts[body["connected_account_id"]]["user_id"]
    assert not flow.transport.smtp_connections and not flow.transport.imap_connections and not flow.transport.sent


@pytest.mark.parametrize("message", [
    "Explain how to export my experiment results to Google Sheets.",
    "Do not export my experiment results to Google Sheets.",
    'Here is an example: "Export my experiment results to Google Sheets".',
])
def test_sheet_export_explanations_negations_and_quoted_examples_never_write(flow, message):
    connect(flow, "googlesheets", "googlesheets")
    founder_reply(flow.client.post(flow.chat_path, json={"message": message}))
    drain()
    assert not flow.transport.writes() and not flow.transport.sent


def test_smtp_mail_readiness_does_not_require_composio_or_gmail_authorization(flow):
    """Your saved SMTP/IMAP account is ready even when app connections have no key and are off."""
    save_apps(flow, key=False, on=False)
    save_mail(flow)
    response = flow.client.get("/api/v1/connections/status")
    card = response.json()["connections"]["smtp"]
    assert response.json()["email_provider"] == "smtp"
    assert card["configured"] and card["ready"], "Your configured local mailbox is incorrectly blocked by Composio"
    assert not card["missing"]
    connected = flow.client.post("/api/v1/connections/gmail/connect", json={})
    assert connected.status_code == 200, connected.text
    assert connected.json()["ok"] and connected.json()["provider"] == "smtp"
    assert not connected.json().get("authorize_url") and not connected.json().get("redirect_url")
    assert not flow.transport.requests and not flow.transport.sent


@pytest.mark.parametrize("configured", [False, True])
def test_requested_mail_connection_uses_smtp_not_composio_oauth(flow, configured):
    """Your email uses saved SMTP settings or names their setup screen, never app OAuth."""
    save_apps(flow, key=False, on=False)
    if configured:
        save_mail(flow)
    reply = founder_reply(flow.client.post(flow.chat_path, json={"message": "Connect my Gmail account"}))
    drain()
    assert "Composio" not in reply["body_md"] and "Connected apps" not in reply["body_md"]
    if configured:
        assert "owner@mail.example" in reply["body_md"] or "connected" in reply["body_md"].lower()
    else:
        assert "Settings > Keys and connections > Your mail account" in reply["body_md"]
        assert "SMTP host" in reply["body_md"] and "mail app password" in reply["body_md"]
    assert "SMTP" in reply["body_md"]
    assert not any("confirmation" in action for action in reply["actions"])
    assert not flow.transport.requests and not flow.transport.sent
    assert not flow.transport.smtp_connections and not flow.transport.imap_connections


@pytest.mark.parametrize("status", [401, 503])
def test_provider_failure_names_your_key_and_reconnect_screen_without_leaking_provider_data(flow, status, caplog):
    """A provider refusal tells you to check your key and app authorization, never exposes provider secrets."""
    save_apps(flow, {"googlesheets": "ac_googlesheets"})
    flow.transport.error_status = status
    response = flow.client.post("/api/v1/connections/googlesheets/connect", json={})
    reply = founder_reply(response)
    assert not reply["ok"]
    assert flow.transport.provider_token not in response.text and flow.transport.provider_token not in caplog.text
    assert "Keys and connections" in reply["message"] and "Connections" in reply["message"]
    assert "Composio key" in reply["message"] and "Google Sheets" in reply["message"]
    assert flow.transport.provider_token not in response.text and flow.transport.provider_token not in caplog.text
    assert not flow.transport.writes() and not flow.transport.sent


def test_requested_discovery_runs_in_local_worker_without_starting_email(flow):
    """Your requested offline discovery runs in the worker, without automatically emailing anyone."""
    from app.db.models import Job
    from app.state import db

    save_mail(flow, send=False)
    response = flow.client.post(flow.journey_path + "/prepare", json={"recruitment_opt_in": True})
    assert response.status_code == 202, response.text
    with db()() as session:
        queued = list(session.scalars(select(Job).where(Job.kind == "gtm.journey.prepare")))
        assert queued, "Your HTTP request did not enqueue local worker work"
    drain(["gtm.journey.prepare"])
    status = flow.client.get(flow.journey_path)
    assert status.status_code == 200, status.text
    state = status.json()
    assert state["prospects"] or state["reachable_leads"] or state["channels"], "Your requested offline discovery produced no saved results"
    assert all(not person["eligible"] for person in state["prospects"])
    assert all(not draft.get("sent_at") for draft in state["drafts"])
    assert not state["can_send"] and state["results"]["sent"] == 0
    assert not flow.transport.sent and not flow.transport.writes()
    with db()() as session:
        assert all(session.get(Job, job.id).status == "succeeded" for job in queued)


def test_connections_require_the_local_owner_credential(flow):
    """A caller without your local owner credential cannot read or change your connections."""
    flow.client.headers.pop("X-Local-Proxy-Secret")
    flow.client.cookies.clear()
    for method, path, body in (("GET", "/api/v1/connections/status", None),
            ("PUT", "/local/settings/composio", {"api_key": flow.transport.secret}),
            ("POST", "/api/v1/connections/googlesheets/connect", {})):
        response = flow.client.request(method, path, json=body)
        assert response.status_code in {401, 403}, "Connections accepted an unauthenticated local caller"
        assert flow.transport.secret not in response.text
    assert not flow.transport.requests and not flow.transport.sent


def test_file_backed_local_app_starts_before_you_configure_connections(local_owner_app, idea):
    """Your actual file-backed local app must open before you can save any app connection."""
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.get("/local/capabilities").status_code == 200
        assert client.post("/ventures", json={"raw_idea": idea, "start": False}).status_code == 201


def test_settings_checks_use_your_smtp_and_imap_without_composio_or_sending(flow):
    """Testing your saved mailbox checks your servers and credentials, without sending a message."""
    save_apps(flow, key=False, on=False)
    save_mail(flow)
    for section in ("smtp", "imap"):
        response = flow.client.post("/local/settings/" + section + "/test")
        assert response.status_code == 200 and response.json()["ok"]
        assert flow.transport.password not in response.text
    assert flow.transport.smtp_connections == [("smtp.mail.example", 587)]
    assert flow.transport.imap_connections == [("imap.mail.example", 993)]
    assert not flow.transport.requests and not flow.transport.sent


def test_invalid_connection_settings_do_not_echo_your_key_or_start_work(flow):
    """An invalid authorization setting asks you to correct it without returning your key."""
    response = flow.client.put("/local/settings/composio", json={"api_key": flow.transport.secret,
        "auth_configs": {"googlesheets": {"token": flow.transport.provider_token}}})
    assert response.status_code == 422
    assert "Keys and connections" in response.text
    assert flow.transport.secret not in response.text and flow.transport.provider_token not in response.text
    assert not flow.transport.requests and not flow.transport.sent


def test_saved_app_key_starts_authorization_without_env_credentials_or_any_tool_execution(flow):
    """Your saved app key and authorization ID start a user-scoped Connect Link without executing tools."""
    save_apps(flow, {"googlesheets": "ac_googlesheets"})
    response = flow.client.post("/api/v1/connections/googlesheets/connect", json={})
    assert response.status_code == 200 and response.json()["ok"]
    assert response.json()["authorize_url"].startswith("https://connect.composio.dev/")
    request = flow.transport.requests[-1]
    assert json.loads(request.content)["auth_config_id"] == "ac_googlesheets"
    assert not flow.transport.writes() and not flow.transport.sent
    assert flow.transport.secret not in response.text


def create_mail_venture(flow):
    response = flow.client.put("/local/providers", json={"provider": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1", "models": {"chat": "offline-fixture"}})
    assert response.status_code == 200
    created = flow.client.post("/ventures", json={"raw_idea": flow.idea, "start": False})
    assert created.status_code == 201, created.text
    flow.venture = created.json()
    assert flow.venture["sample"] is False
    flow.client.headers["X-Workspace-Id"] = flow.venture["workspace_id"]
    flow.chat_path = f"/ventures/{flow.venture['id']}/chat"
    flow.journey_path = f"/api/v1/ventures/{flow.venture['id']}/gtm/journey"


def email_request(flow, monkeypatch):
    """Seed already reviewed owner work; execution and admission still go through the HTTP API."""
    from app.db.models import GtmContact, GtmEmailAccount, VentureDocument
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    create_mail_venture(flow)
    save_mail(flow)
    now = utcnow()
    from app.gtm.supymem.composio_gmail import user_id_for

    with db()() as session:
        account = GtmEmailAccount(id="owner_mailbox", workspace_id=flow.venture["workspace_id"], provider="smtp",
            email="owner@mail.example", connected_account_id="smtp_owner_mailbox",
            composio_user_id=user_id_for(flow.venture["workspace_id"]), app_user_id=user_id_for(flow.venture["workspace_id"]),
            status="active", created_at=now, updated_at=now)
        session.add(account)
        sender = {"mailbox_id": account.id, "email": account.email, "connected_account_id": account.connected_account_id,
            "composio_user_id": account.composio_user_id, "owner_workspace_id": account.workspace_id, "status": account.status}
        contact = GtmContact(id="owner_contact", workspace_id=flow.venture["workspace_id"], email="participant@research.example",
            first_name="Avery", email_confidence="risky", source=f"venture:{flow.venture['id']}:import",
            contact_provenance="founder_import", lawful_basis="consent", lawful_basis_at=now,
            lawful_basis_note="You recorded this person's consent to receive your interview invitation.", recipient_geo="US",
            enrichment_json=json.dumps({"founder_import": {"lawful_basis_attested": True, "email_check": "mx"}}),
            stage_changed_at=now, created_at=now)
        session.add(contact)
        draft = {"contact_id": contact.id, "to": contact.email, "subject": "Your interview invitation",
            "body": 'Would you like to share your experience?\n\nI have your address from my own contacts.\n\n'
                'Your research team\n42 Observatory Road, Portland, OR 97201\n\n'
                'If this is not relevant, just reply "no thanks" and I will not email you again.',
            "state": "draft", "review_state": "approved", "channel": "email", "day": 1,
            "batch_id": "owner_batch", "founder_import": True,
            "requires_batch_approval": True, "requires_journey_review": True}
        session.commit()
    mailbox_state = flow.client.get(flow.journey_path).json()["gmail"]
    if mailbox_state.get("multi_gmail_v1"):
        draft.update(sender=sender, mailbox_id=sender["mailbox_id"])
    with db()() as session:
        session.add(VentureDocument(id="owner_draft", venture_id=flow.venture["id"], kind="outreach_draft",
            title="Your reviewed invitation", body_md=draft["body"], provenance_json=json.dumps(draft), created_at=now, updated_at=now))
        session.commit()
    state = flow.client.get(flow.journey_path).json()
    batch = next(batch for batch in state["batches"] if batch["batch_id"] == "owner_batch")
    return {name: batch[name] for name in ("batch_id", "draft_ids", "approval_digest", "cap")}


def test_requested_reviewed_email_uses_only_your_smtp_and_keeps_its_compliance_footer(flow, monkeypatch):
    """A specifically requested, enabled email goes through your SMTP, with disclosure, address and opt-out."""
    request = email_request(flow, monkeypatch)
    response = flow.client.post(flow.journey_path + "/send", json=request)
    require_local_send_pricing(response)
    assert response.status_code == 200, response.text
    drain(["gtm.outreach.retry"])
    assert len(flow.transport.sent) == 1, "Your requested enabled email did not reach your SMTP server"
    message = flow.transport.sent[0]
    text = message.get_content()
    assert "I have your address from my own contacts." in text
    assert 'reply "no thanks"' in text and "42 Observatory Road, Portland, OR 97201" in text
    assert "owner@mail.example" in message["From"]
    assert not flow.transport.requests and not flow.transport.writes()
    assert flow.transport.password not in response.text


@pytest.mark.parametrize("restriction", ["suppression", "opt_out", "cap", "disclosure", "unsubscribe", "guessed", "off"])
def test_requested_email_cannot_bypass_suppression_caps_compliance_or_your_switch(flow, monkeypatch, restriction):
    """Even your explicit send request cannot bypass an opt-out, send cap, compliant footer or disabled sending."""
    from app.db.models import GtmContact, GtmSuppression, GtmTouch, VentureDocument
    from app.gtm.relationships.suppression import email_suppressed
    from app.state import db
    from core.clock import utcnow

    request = email_request(flow, monkeypatch)
    now = utcnow()
    with db()() as session:
        contact = session.get(GtmContact, "owner_contact")
        if restriction == "suppression":
            session.add(GtmSuppression(id="owner_suppression", workspace_id=contact.workspace_id,
                email=contact.email, source="opt_out", created_at=now))
        elif restriction == "opt_out":
            contact.unsubscribed_at, contact.do_not_contact = now, 1
        elif restriction == "cap":
            from app.autopilot.guard import mailbox_send_max
            from app.db.models import GtmEmailAccount

            limit = mailbox_send_max(session, session.get(GtmEmailAccount, "owner_mailbox"))
            for index in range(limit):
                previous = GtmContact(id=f"previous_contact_{index}", workspace_id=contact.workspace_id,
                    email=f"participant@history-{index}.example", source=f"venture:{flow.venture['id']}:import",
                    stage_changed_at=now, created_at=now)
                session.add(previous)
                session.flush()
                session.add(GtmTouch(id=f"previous_send_{index}", workspace_id=contact.workspace_id,
                    venture_id=flow.venture["id"], contact_id=previous.id, direction="out", channel="email",
                    logged_by="outreach:" + flow.venture["id"], simulated=0, sent_at=now, created_at=now))
        elif restriction == "guessed":
            evidence = json.loads(contact.enrichment_json)
            evidence["email_route"] = {"route_type": "guess"}
            contact.enrichment_json = json.dumps(evidence)
            assert email_suppressed(session, contact.workspace_id, contact)
        elif restriction in {"disclosure", "unsubscribe"}:
            document = session.get(VentureDocument, "owner_draft")
            draft = json.loads(document.provenance_json)
            removed = "I have your address from my own contacts." if restriction == "disclosure" else (
                'If this is not relevant, just reply "no thanks" and I will not email you again.')
            draft["body"] = draft["body"].replace(removed, "")
            document.provenance_json = json.dumps(draft)
        session.commit()
    if restriction == "off":
        assert flow.client.put("/local/capabilities/send_email", json={"on": False}).status_code == 200
    state = flow.client.get(flow.journey_path).json()
    batch = next(batch for batch in state["batches"] if batch["batch_id"] == request["batch_id"])
    request = {name: batch[name] for name in ("batch_id", "draft_ids", "approval_digest", "cap")}
    response = flow.client.post(flow.journey_path + "/send", json=request)
    require_local_send_pricing(response)
    assert response.status_code in {200, 409, 422}, response.text
    expected = {"suppression": "suppressed_or_replied", "opt_out": "suppressed_or_replied",
        "disclosure": "gtm_source_disclosure_required", "unsubscribe": "gtm_draft_one_ask_and_optout_required",
        "guessed": "suppressed_or_replied"}
    if restriction in expected:
        assert response.status_code == 409 and response.json()["detail"]["code"] == expected[restriction], response.json().get("detail")
    elif restriction == "cap":
        assert "limit" in json.dumps(response.json()).lower()
    else:
        assert "Settings" in json.dumps(response.json()) and "Send email" in json.dumps(response.json())
    drain(["gtm.outreach.retry"])
    assert not flow.transport.sent, "Your requested email bypassed a sending safety check"
    assert not flow.transport.requests and not flow.transport.writes()


def test_fresh_mailbox_verification_saves_a_durable_local_binding_without_composio_flags(flow, monkeypatch):
    """Your newly configured mailbox verifies and keeps a stable binding without a hosted policy switch."""
    from app.db.models import GtmEmailAccount
    from app.state import db

    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    create_mail_venture(flow)
    save_mail(flow)
    response = flow.client.post("/api/v1/connections/gmail/verify", json={"venture_id": flow.venture["id"]})
    assert response.status_code == 200
    assert response.json()["ok"] and response.json()["status"] == "verified", "Your saved local mailbox did not verify"
    with db()() as session:
        bindings = list(session.scalars(select(GtmEmailAccount).where(GtmEmailAccount.workspace_id == flow.venture["workspace_id"])))
        assert len(bindings) == 1 and bindings[0].provider == "smtp"
    assert not flow.transport.sent and not flow.transport.requests


def test_connection_state_and_local_settings_never_return_log_or_store_plaintext_keys(flow, tmp_path, caplog):
    """Your keys stay encrypted on disk and absent from settings, connection state and logs."""
    save_apps(flow, {"googlesheets": "ac_googlesheets"})
    save_mail(flow)
    private = (flow.transport.secret, flow.transport.password, flow.transport.provider_token)
    for path in ("/local/settings/composio", "/local/settings/smtp", "/local/settings/catalog",
            "/local/capabilities", "/api/v1/connections/status"):
        response = flow.client.get(path)
        assert response.status_code == 200
        leaked = any(secret in response.text for secret in private)
        assert not leaked, "Your settings or connection-state API returned a secret"
    logged = any(secret in caplog.text for secret in private)
    assert not logged, "Your connection logs exposed a secret"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            plaintext = any(secret.encode() in path.read_bytes() for secret in private)
            assert not plaintext, "Your app wrote a connection key or mailbox password in plaintext"
