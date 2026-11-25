"""Your coding tool owns page creation; your laptop owns publication and signups.

Tool output and external transports are fixtures. Requests use the real owner
credential, SQLite, saved settings and local worker's normal lease loop.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import socket
import zipfile
from contextlib import ExitStack
from html import escape, unescape
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from tests.unit.test_local_mode import _owner_client, local_owner_app


DOMAINS = [
    pytest.param((
        "A stock rotation planner for independent grocery stores",
        "Grocery store managers reducing expired inventory",
        "grocery",
        ), id="b2b-grocery",
    ),
    pytest.param((
        "A tide-aware beach outing planner for families",
        "Parents planning safe beach outings with young children",
        "beach",
        ), id="consumer-beach",
    ),
]


class ExternalTransports:
    def __init__(self):
        self.requests = []
        self.public_files = {}
        self.storage = {}
        self.mail = []
        self.incoming_mail = []
        self.account = {}
        self.waitlist_form = None
        self.events_reachable = False

    def http(self, request):
        host = urlsplit("https://" + request.headers.get("host", request.url.host)).hostname.lower().rstrip(".")
        self.requests.append((request.method, str(request.url)))
        assert host != "supymem.com" and not host.endswith(".supymem.com"), "Your laptop must never contact a Supymem host."
        if host == "api.vercel.com" and request.method == "POST":
            payload = json.loads(request.read())
            if request.headers.get("authorization") != "Bearer " + self.account["token"]:
                raise AssertionError("Your publication must use the token you saved in the app.")
            assert payload["project"] == payload["name"] == self.account["project"]
            self.public_files = {item["file"]: base64.b64decode(item["data"]) for item in payload["files"]}
            return httpx.Response(200, json={"id": "owner-deploy", "readyState": "READY", "url": "owner-site.example"})
        if host == "api.netlify.com" and request.method == "POST":
            if request.headers.get("authorization") != "Bearer " + self.account["token"]:
                raise AssertionError("Your publication must use the token you saved in the app.")
            assert request.url.path == f"/api/v1/sites/{self.account['project']}/deploys"
            with zipfile.ZipFile(io.BytesIO(request.read())) as archive:
                self.public_files = {name: archive.read(name) for name in archive.namelist()}
            return httpx.Response(200, json={"id": "owner-deploy", "state": "ready", "ssl_url": "https://owner-site.example"})
        if host == "owner-site.example":
            if request.url.path.startswith("/public/waitlists/"):
                return httpx.Response(200, text=self.waitlist_form) if self.waitlist_form else httpx.Response(404)
            if request.url.path == "/e":
                return httpx.Response(204 if self.events_reachable else 404)
            path = request.url.path.lstrip("/") or "index.html"
            return httpx.Response(200, content=self.public_files[path]) if path in self.public_files else httpx.Response(404)
        if host == "cloudflare-dns.com":
            return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 15, "data": "10 mx.audience.example."}]})
        raise AssertionError("Your page flow attempted an unexpected external HTTP request.")

    def s3(self, operation, request):
        self.requests.append((operation.name, request["url"]))
        path = urlsplit(request["url"]).path
        assert urlsplit(request["url"]).hostname.startswith(self.account["bucket"] + ".")
        assert path.startswith("/" + self.account["prefix"] + "/")
        if operation.name == "PutObject":
            body = request["body"]
            from botocore.httpchecksum import AwsChunkedWrapper

            if isinstance(body, AwsChunkedWrapper):
                # shortcut: this fake uses botocore's raw body, decode wire chunks if its wrapper changes.
                body = body._raw
            content = body.read() if hasattr(body, "read") else body
            digest = hashlib.sha256(content).hexdigest()
            self.storage[path] = (content, digest)
            self.public_files[path.split("/", 2)[-1]] = content
            return SimpleNamespace(status_code=200, headers={}), {"ETag": '"owner-file"'}
        if operation.name == "HeadObject":
            content, digest = self.storage[path]
            return SimpleNamespace(status_code=200, headers={}), {"ContentLength": len(content), "Metadata": {"sha256": digest}}
        raise AssertionError("Your page flow attempted an unexpected storage operation.")


@pytest.fixture(params=DOMAINS)
def page_flow(request, local_owner_app, monkeypatch, caplog, sample_provider):
    from desktop_runtime import agent_status
    from app import coding_agents_consent

    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {})
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "0")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    for name in ("VERCEL_TOKEN", "NETLIFY_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                 "AWS_SESSION_TOKEN", "SUPYMEM_PUBLIC_URL", "PUBLIC_GATEWAY_URL", "PUBLIC_EVENTS_URL",
                 "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    external = ExternalTransports()

    def handle_http(transport, http_request):
        return external.http(http_request)

    async def handle_async_http(transport, http_request):
        await http_request.aread()
        return external.http(http_request)

    def no_connection(connection, address):
        raise AssertionError("Your offline page flow must not open a network connection.")

    def no_datagram(connection, *arguments):
        raise AssertionError("Your offline page flow must not send a network datagram.")

    def public_address(host, port, *arguments, **options):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_http)
    monkeypatch.setattr(socket.socket, "connect", no_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", no_connection)
    monkeypatch.setattr(socket.socket, "sendto", no_datagram)
    monkeypatch.setattr(socket, "getaddrinfo", public_address)
    from botocore.endpoint import Endpoint

    monkeypatch.setattr(Endpoint, "make_request", lambda endpoint, operation, payload: external.s3(operation, payload))
    import imaplib
    import smtplib

    class Mailbox:
        def __init__(self, *arguments, **options):
            external.incoming_mail.append("connected")

        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            return None

        def send_message(self, message, **options):
            external.mail.append(message)
            return {}

        def sendmail(self, sender, recipients, message, **options):
            external.mail.append(message)
            return {}

        def login(self, *arguments):
            return "OK", []

        def starttls(self, **options):
            return 220, b"ready"

        def ehlo(self):
            return 250, b"ready"

        def quit(self):
            return 221, b"closed"

        def logout(self):
            return "BYE", []

        def select(self, *arguments, **options):
            return "OK", [b"0"]

        def search(self, *arguments):
            return "OK", [b""]

    monkeypatch.setattr(smtplib, "SMTP", Mailbox)
    monkeypatch.setattr(smtplib, "SMTP_SSL", Mailbox)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", Mailbox)
    with ExitStack() as owner_stack:
        client = owner_stack.enter_context(_owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)))
        flow = PageFlow(client, external, *request.param)
        if sample_provider:
            flow.request("POST", "/local/providers/sample", json={})
        elif sample_provider is False:
            flow.request("POST", "/local/providers", json={"provider": "openai_compatible",
                "base_url": "http://127.0.0.1:11434/v1", "models": {"chat": "local-chat", "planning": "local-chat"}})
        created = flow.request("POST", "/ventures", json={"raw_idea": flow.idea, "start": False}, status=201).json()
        flow.venture_id, flow.workspace_id = created["id"], created["workspace_id"]
        client.headers["X-Workspace-Id"] = flow.workspace_id
        yield flow
        flow.drain()
        assert not external.mail, "Your page work must not send mail that you did not request."
        assert not external.incoming_mail, "Your page work must not read your mailbox without a request."
        assert all(secret not in caplog.text for secret in flow.secrets), "Your saved credentials must never be logged."
        from sqlalchemy import select
        from app.db.models import GtmTouch
        from app.state import db

        with db()() as session:
            assert not list(session.scalars(select(GtmTouch).where(GtmTouch.sent_at.is_not(None)))), "Your page requests must not silently record a sent outreach message."


@pytest.fixture
def sample_provider(request):
    return getattr(request, "param", False)


class PageFlow:
    def __init__(self, client, external, idea, audience, topic):
        self.client, self.external = client, external
        self.idea, self.audience = idea, audience
        self.topic = topic
        self.secrets = []
        self.idea_confirmed = False
        self.venture_id = self.workspace_id = None

    def path(self, suffix):
        return f"/ventures/{self.venture_id}{suffix}"

    def request(self, method, path, *, status=200, **options):
        response = self.client.request(method, path, **options)
        assert all(secret.encode() not in response.content for secret in self.secrets), "Your API response must not disclose saved credentials."
        assert response.status_code == status, f"{method} {path}: HTTP {response.status_code}, expected {status}."
        return response

    def drain(self):
        from sqlalchemy import select
        from app.db.models import Job
        from app.state import db
        from app.worker.main import run_worker

        for attempt in range(16):
            async def once():
                await run_worker(once=True, stop=asyncio.Event())

            asyncio.run(once())
            with db()() as session:
                pending = list(session.scalars(select(Job).where(Job.status == "pending")))
            if not pending:
                return
        pytest.fail("Your requested local jobs did not drain through the worker loop.")

    def chat(self, text):
        reply = self.request("POST", self.path("/chat"), json={"message": text, "idempotency_key": uuid4().hex}).json()
        self.drain()
        history = self.request("GET", self.path("/chat")).json()["items"]
        for message in [reply, *(message for message in history if message.get("role") == "assistant")]:
            assert message.get("kind") != "confirmation", "Your requested work must not ask you to confirm it again."
            assert not any(action.get("confirmation") for action in message.get("actions", [])), "Your requested work must not show a confirmation card."
            assert not re.search(r"\b\d+(?:[.,]\d+)?\s*credits?\b|\bcredits?\s*[:=]\s*\d", message.get("body_md", ""), re.I), "Your reply must not show credit counts."
        return reply

    def confirm_idea(self):
        if self.idea_confirmed:
            return
        opened = self.request("POST", self.path("/idea/sessions"), json={}, status=201).json()
        path = self.path(f"/idea/sessions/{opened['id']}")
        self.drain()
        self.request("POST", path + "/messages", json={"text": self.audience})
        self.drain()
        self.request("POST", path + "/messages", json={"text": "Skip the remaining questions"})
        self.drain()
        review = self.request("GET", path).json()
        assert review["state"] == "reviewing", "Your answers must reach the idea review before you build."
        self.request("POST", path + "/confirm", json={"revision_id": review["current_revision"]["id"], "start_research": False})
        self.drain()
        self.idea_confirmed = True

    def import_tool_page(self, *, deployed=False):
        from app.conversion import waitlist
        from app.firstlook.service import ingest_builder_page
        from app.pagebuilder import local_publish
        from tests.unit.product.test_static_publication import _signup_fixture

        self.confirm_idea()
        self.request("PUT", self.path("/waitlist"), json={"enabled": True})
        name = waitlist.public_details(self.venture_id)["name"]
        destination = local_publish.waitlist_destination(self.venture_id, name, deployed=deployed)
        endpoint = urlsplit(destination["url"])
        origin = "mailto:" if endpoint.scheme == "mailto" else endpoint.scheme + "://" + endpoint.netloc if endpoint.netloc else "'self'"
        target = destination["url"] if destination["mode"] == "endpoint" else "waitlist.html"
        html = (f'<!doctype html><html lang="en"><head><title>{escape(self.idea)}</title>'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta http-equiv="Content-Security-Policy" '
                f'content="default-src \'none\'; base-uri \'none\'; script-src \'none\'; style-src \'unsafe-inline\'; form-action {origin}">'
                '<style>body{background:#fff;color:#111}</style></head><body><main>'
                f'<h1>{escape(self.idea)}</h1><p>{escape(self.audience)}</p>'
                '<section id="waitlist"><h2>Explore early access</h2>'
                f'<p>{escape(destination["notice"])}</p>'
                f'<a id="waitlist_cta" href="{escape(target, quote=True)}">Get in touch</a></section>'
                '</main></body></html>')
        companion = html if destination["mode"] == "setup" else _signup_fixture(name, destination["url"],
            netlify_name="waitlist-" + self.venture_id if destination["mode"] == "netlify" else "")
        result = ingest_builder_page(self.venture_id, html, {"waitlist.html": companion})
        assert result["ok"], result
        self.tool_html = html
        return self.request("GET", f"/workspaces/{self.workspace_id}/site/preview", params={"venture_id": self.venture_id}).json()["html"]

    def switch(self, name, on):
        return self.request("PUT", f"/local/capabilities/{name}", json={"on": on}).json()

    def publishing(self, provider, missing=None):
        values = {"provider": provider, "project": "owner-project", "waitlist_email": "updates@owner.example"}
        if provider == "s3":
            values.update(bucket="owner-bucket", region="us-east-1", prefix="pages", site_url="https://owner-site.example",
                          access_key_id="owner-access-sentinel", secret_access_key="owner-secret-sentinel", session_token="owner-session-sentinel")
        else:
            values["token"] = "owner-publishing-sentinel"
        if missing:
            values.pop(missing)
        self.external.account = values
        self.secrets.extend(value for field, value in values.items() if field in {"token", "access_key_id", "secret_access_key", "session_token"})
        return self.request("PUT", "/local/settings/publishing", json=values).json()

    def app(self):
        created = self.request("POST", self.path("/apps"), status=201,
                               json={"name": "Your page", "slug": "owner-page", "kind": "service"}).json()
        return created.get("app", created)["id"]

    def counts(self, **changes):
        return {"path": "/", "views": 120, "clicks": 18, "signups": 6,
                "start_date": "2026-11-01", "end_date": "2026-11-07", **changes}

    def import_contact(self, address):
        path = self.path("/gtm/contacts/import")
        preview = self.request("POST", path + "/preview", json={"text": address, "format": "paste"}).json()
        saved = self.request("POST", path, json={"preview_id": preview["preview_id"],
            "lawful_basis_attested": True, "lawful_basis": "consent"}).json()
        return saved["contact_ids"][0]


def test_opening_your_venture_starts_no_page_or_outreach_work(page_flow):
    """Reading your app must not build, publish, search, or contact visitors."""
    flow = page_flow
    flow.drain()
    flow.request("GET", flow.path("/site/local"))
    flow.request("GET", flow.path("/waitlist"))
    flow.request("GET", flow.path("/apps"))
    from sqlalchemy import select
    from app.db.models import Job, GtmTouch
    from app.state import db

    with db()() as session:
        assert not list(session.scalars(select(Job).where(Job.kind.in_(["pagebuilder.build", "hosting.publish"]))))
        assert not list(session.scalars(select(GtmTouch)))
    assert not flow.external.requests


@pytest.mark.parametrize("domain", DOMAINS)
def test_page_qa_failure_names_rejected_checks_without_echoing_page_content(monkeypatch, domain):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.pagebuilder.local import failure_reason

    idea, audience, topic = domain
    failure = "pagebuilder_qa_failed:section_missing:hero,required_text_missing:" + idea + ",active_content:script,honest_copy:unreviewed_visible_copy:" + audience
    message = failure_reason(failure)
    assert "sections" in message and "required text" in message and "active content" in message and "reviewed copy" in message
    assert idea not in message and audience not in message and topic not in message
    assert "Website" in message and "Retry" in message


def test_tool_page_preview_html_and_zip_stay_private(page_flow):
    """Your tool's saved output previews and exports without a hosting account."""
    flow = page_flow
    preview = flow.import_tool_page()
    exported = flow.request("GET", flow.path("/site/local/export.html"))
    zipped = flow.request("GET", flow.path("/site/local/export.zip"))
    assert "<html" in preview.lower() and "<html" in exported.text.lower()
    assert exported.headers["content-type"].startswith("text/html")
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        assert "index.html" in archive.namelist()
        assert "<html" in archive.read("index.html").decode().lower()
        assert not any(name.endswith((".env", ".db", ".sqlite")) for name in archive.namelist())
    assert not flow.external.requests
    assert flow.request("GET", flow.path("/site/local")).json()["publication"] is None


@pytest.mark.parametrize("sample_provider", [True], indirect=True)
def test_page_preview_keeps_your_venture_and_audience(page_flow):
    """Your preview reflects your own venture, not a generic early-access page."""
    flow = page_flow
    preview = flow.import_tool_page()
    exported = flow.request("GET", flow.path("/site/local/export.html"))
    assert flow.topic in preview.casefold() and flow.topic in exported.text.casefold(), "Your page must preserve your venture's audience, not another sample domain."
    assert flow.audience in preview and flow.audience in exported.text


@pytest.mark.parametrize("provider", ["vercel", "netlify", "s3"])
def test_requested_publication_uses_only_your_saved_account(page_flow, provider):
    """An explicit publish request uses your saved host and posts a verified link."""
    flow = page_flow
    flow.publishing(provider)
    flow.import_tool_page(deployed=True)
    assert flow.switch("publish_pages", True)["state"] == "ready"
    assert not flow.external.public_files
    reply = flow.chat("Publish my landing page now.")
    assert "https://owner-site.example" in reply["body_md"], "Your publish reply must give you the verified page link."
    publication = flow.request("GET", flow.path("/site/local")).json()["publication"]
    assert publication["provider"] == provider and publication["state"] == "live"
    assert flow.external.public_files["index.html"]
    assert all(secret.encode() not in content for secret in flow.secrets for content in flow.external.public_files.values())


def test_your_publishing_credentials_are_private_on_disk_and_in_exports(page_flow):
    """Saving your account encrypts credentials; neither export contains them."""
    flow = page_flow
    flow.publishing("s3")
    flow.import_tool_page()
    flow.request("GET", "/local/settings/publishing")
    flow.request("GET", flow.path("/site/local/export.html"))
    zipped = flow.request("GET", flow.path("/site/local/export.zip"))
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        assert all(secret.encode() not in archive.read(name) for name in archive.namelist() for secret in flow.secrets)
    from app.config import data_root

    for path in data_root().rglob("*"):
        if path.is_file():
            assert all(secret.encode() not in path.read_bytes() for secret in flow.secrets), "Your credentials must never be stored in plaintext."


@pytest.mark.parametrize("sample_provider", [None, False, True], indirect=True)
@pytest.mark.parametrize("reviewed", [False, True], ids=["new-idea", "reviewed-idea"])
def test_requested_build_without_a_tool_offers_connection_and_starts_no_work(page_flow, monkeypatch, reviewed):
    from sqlalchemy import select
    from app.db.models import Job
    from app.pagebuilder import pipeline
    from app.state import db

    flow = page_flow
    if reviewed:
        flow.confirm_idea()
    flow.publishing("vercel")
    flow.switch("publish_pages", True)
    monkeypatch.setattr(pipeline, "generate_page", lambda *args, **kwargs: pytest.fail("Your internal page generator ran."))
    reply = flow.chat("Build and publish my landing page with a waitlist form.")
    assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
    assert any(action.get("href") for action in reply["actions"])
    assert flow.request("GET", flow.path("/site/local")).json()["publication"] is None
    flow.request("GET", f"/workspaces/{flow.workspace_id}/site/preview",
                 params={"venture_id": flow.venture_id}, status=404)
    with db()() as session:
        assert not list(session.scalars(select(Job).where(Job.kind.in_(["pagebuilder.build", "hosting.publish"]))))
    assert not flow.external.requests


def _probe_page_actions(html):
    # shortcut: probe native action routing without browser geometry; use live QA to verify focus and layout.
    from app.pagebuilder import qa, rendering

    parser = qa.PageParser()
    parser.feed(html)
    activated = []
    links = []
    def evaluate(script):
        if "addEventListener" in script:
            activated.append("intercept")
        return True
    for tag, attributes, _parents in parser.elements:
        if tag == "a" and attributes.get("href"):
            links.append(SimpleNamespace(get_attribute=lambda name, attributes=attributes: attributes.get(name),
                is_visible=lambda: True, is_enabled=lambda: True, evaluate=evaluate,
                click=lambda **_options: activated.append("click")))
    def anchor(_script, *, arg, **_options):
        assert any(attributes.get("id") == arg for _tag, attributes, _parents in parser.elements)
        assert all(link.get_attribute("target") != "_blank" for link in links if link.get_attribute("href") == "#" + arg)
    page = SimpleNamespace(locator=lambda _selector: SimpleNamespace(count=lambda: len(links), nth=lambda index: links[index]),
        goto=lambda *_arguments, **_options: None, evaluate=lambda _script: True,
        wait_for_function=anchor, wait_for_url=lambda *_arguments, **_options: None,
        keyboard=SimpleNamespace(press=lambda key: activated.append(key)))
    return rendering._link_actions(page, "http://localhost:9000"), activated


@pytest.mark.parametrize("mode", ["local", "hosted"])
@pytest.mark.parametrize("target", ["mailto:updates@owner.example", "#waitlist"])
def test_renderer_signup_actions_activate_mailto_and_visible_anchor(monkeypatch, mode, target):
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    html = f'<a id="waitlist_cta" href="{target}">Get in touch</a><section id="waitlist">Signups are not connected yet.</section>'
    actions, activated = _probe_page_actions(html)
    assert len(actions) == 2 and all(action["passed"] for action in actions)
    assert "click" in activated and "Enter" in activated
    assert ("intercept" in activated) == target.startswith("mailto:")
    rejected, _activated = _probe_page_actions(html.replace(target, "/public/waitlists/owner"))
    assert all(action["error"] == "unsupported_target" for action in rejected)


@pytest.mark.parametrize("topic", ["Equipment inspection scheduling", "Weekend pottery classes"])
def test_private_preview_control_checker_does_not_change_tool_source(monkeypatch, topic):
    from app.pagebuilder import qa
    from core.static_qa_contracts import Action, Contract

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    notice = "Preview only. This page does not collect signups."
    contract = Contract(profile="static_landing_v1", product_name=topic,
        required_text=[notice], actions=[Action(id="waitlist_cta", kind="anchor",
            role="link", name="Get in touch", target="waitlist")])
    html = f'<section id="waitlist"><h2>{topic}</h2><a id="waitlist_cta" href="/public/waitlists/owner" target="_blank">Get in touch</a></section>'
    result = qa.pin_control_names(html, contract)
    assert result == html
    parser = qa.PageParser()
    parser.feed(result)
    assert notice not in parser.section_text["waitlist"]
    assert qa.pin_control_names(result, contract) == result
    actions, _activated = _probe_page_actions(result)
    assert len(actions) == 2 and all(not action["passed"] for action in actions)
    tool_result = f'<section id="waitlist"><h2>{topic}</h2><p>{notice}</p><a id="waitlist_cta" href="#waitlist" aria-label="Get in touch">Get in touch</a></section>'
    assert qa.pin_control_names(tool_result, contract) == tool_result
    actions, _activated = _probe_page_actions(tool_result)
    assert len(actions) == 2 and all(action["passed"] for action in actions)


@pytest.mark.parametrize("mode", ["local", "hosted"])
@pytest.mark.parametrize("domain", ["stock-planning", "family-camping"])
def test_renderer_invalid_signup_url_does_not_log_credentials(monkeypatch, caplog, mode, domain):
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    secret = uuid4().hex
    html = f'<a id="waitlist_cta" href="https://owner:{secret}@{domain}.example/public/waitlists/owner">Get in touch</a>'
    actions, _activated = _probe_page_actions(html)
    assert all(not action["passed"] and action["error"] == "unsupported_target" for action in actions)
    assert secret not in repr(actions)
    assert all(secret not in repr(record.__dict__) for record in caplog.records)


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_tool_checker_rejects_incomplete_markup_without_generating_a_replacement(monkeypatch, domain):
    from tests.unit.pagebuilder.test_pipeline import customer_draft, final_plan, fixture_html
    from tests.unit.pagebuilder.test_tool_build import _with_social_preview
    from app.pagebuilder import images, local, models, pipeline, tool

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data, draft = customer_draft(domain)
    inputs = models.PageInput.model_validate(data)
    plan = final_plan({"models": models, "pipeline": pipeline, "images": images}, inputs, draft)
    files = {asset.path: asset.svg.encode() for asset in plan.assets}
    incomplete = b'<!doctype html><html lang="en"><head><title>Incomplete page</title></head><body><p>Missing your reviewed copy.</p></body></html>'
    files["index.html"] = incomplete
    monkeypatch.setattr(pipeline, "generate_page", lambda *args, **kwargs: pytest.fail("Your internal page generator ran."))
    rejected = tool.check_output(files, inputs, plan)
    assert not rejected["passed"] and "section_missing:hero" in rejected["failures"]
    assert files["index.html"] == incomplete
    message = local.failure_reason("pagebuilder_qa_failed:" + ",".join(rejected["failures"]))
    assert "missing text from your reviewed plan" in message and "Website" in message and "Retry" in message
    assert inputs.idea not in message and plan.brief.audience not in message
    files["index.html"] = fixture_html(plan, inputs).encode()
    files = _with_social_preview(files, inputs)
    assert tool.check_output(files, inputs, plan)["passed"]
    brief = tool.build_brief(inputs, plan)
    for value in (inputs.idea, plan.brief.audience, plan.brief.promise, plan.brief.cta, inputs.waitlist_url):
        assert value in brief


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("mode", ["setup", "endpoint", "netlify", "mailto"])
def test_tool_checker_keeps_saved_signup_settings_and_rejects_changed_files(monkeypatch, domain, mode):
    from tests.unit.pagebuilder.test_pipeline import customer_draft, final_plan, fixture_html
    from tests.unit.pagebuilder.test_tool_build import _with_social_preview
    from app.pagebuilder import images, local_publish, models, pipeline, tool

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data, draft = customer_draft(domain)
    destination = {"mode": mode, "url": "https://owner.example/join" if mode == "endpoint" else
                   "mailto:updates@owner.example" if mode == "mailto" else "/" if mode == "netlify" else "",
                   "notice": "Your signup destination."}
    inputs = models.PageInput.model_validate({**data, "research": {"local_signup": destination}})
    plan = final_plan({"models": models, "pipeline": pipeline, "images": images}, inputs, draft)
    files = {asset.path: asset.svg.encode() for asset in plan.assets}
    files["waitlist.html"] = local_publish.signup_page(inputs.venture_id, inputs.name, destination).encode()
    files["index.html"] = fixture_html(plan, inputs).encode()
    files = _with_social_preview(files, inputs)
    checked = tool.check_output(files, inputs, plan)
    assert checked["passed"], checked["failures"]
    signup = files["waitlist.html"].decode()
    if mode == "setup":
        assert "<form" not in signup
    else:
        assert 'name="consent"' in signup and 'name="consent_digest"' in signup
        assert 'name="website"' in signup
        assert ('data-netlify="true"' if mode == "netlify" else destination["url"]) in signup
    files["waitlist.html"] = b"<html>Your request was sent.</html>"
    assert "local_signup_changed" in tool.check_output(files, inputs, plan)["failures"]

@pytest.mark.parametrize("capability,text", [
    ("landing_pages", "Build my landing page."),
    ("publish_pages", "Publish my landing page now."),
    ("page_measurement", "Record page counts: {}"),
])
def test_switched_off_features_name_the_exact_switch_and_start_no_work(page_flow, capability, text):
    """A disabled feature names Settings > Capabilities and your actual switch."""
    flow = page_flow
    if capability == "publish_pages":
        flow.publishing("vercel")
    blocked = flow.switch(capability, False)
    before = list(flow.external.requests)
    reply = flow.chat(text)
    assert blocked["next_step"] in reply["body_md"], "Your blocked reply must name the exact feature switch."
    assert "Settings > Capabilities" in reply["body_md"] and blocked["title"] in reply["body_md"]
    assert flow.external.requests == before


@pytest.mark.parametrize("provider,missing", [
    (None, "provider"),
    ("vercel", "token"), ("vercel", "project"),
    ("netlify", "token"), ("netlify", "project"),
    ("s3", "bucket"), ("s3", "region"), ("s3", "prefix"), ("s3", "site_url"),
    ("s3", "access_key_id"), ("s3", "secret_access_key"),
])
def test_missing_publish_setting_names_the_field_and_screen(page_flow, provider, missing):
    """Your publish reply identifies the exact missing saved account field."""
    flow = page_flow
    flow.publishing(provider, missing)
    blocked = flow.switch("publish_pages", True)
    assert blocked["state"] == "needs_setup"
    reply = flow.chat("Publish my landing page now.")
    assert blocked["next_step"] in reply["body_md"]
    assert "Keys and connections" in reply["body_md"]
    assert not flow.external.public_files and not flow.external.storage


@pytest.mark.parametrize("sample_provider", [None])
def test_missing_coding_tool_names_your_connection_screen(page_flow):
    """Without a connected tool, your page request names the tool to connect."""
    flow = page_flow
    blocked = flow.switch("landing_pages", True)
    assert blocked["state"] == "needs_setup"
    assert blocked["missing"] and all("coding tool" in field.lower() for field in blocked["missing"])
    reply = flow.chat("Build my landing page.")
    assert blocked["next_step"] in reply["body_md"]
    assert "Keys and connections > Coding tools" in reply["body_md"]
    assert not flow.external.requests


@pytest.mark.parametrize("mode", [
    "endpoint",
    "mailto", "netlify", "setup",
])
def test_waitlist_destination_and_export_explain_where_signups_go(page_flow, mode):
    """Your destination names signup setup; private exports keep your tool's bytes."""
    flow = page_flow
    flow.confirm_idea()
    values = {"provider": "netlify" if mode == "netlify" else "vercel"}
    if mode == "endpoint":
        from app.pagebuilder.local_publish import signup_page
        from app.conversion.waitlist import public_details

        flow.request("PUT", flow.path("/waitlist"), json={"enabled": True})
        flow.external.waitlist_form = signup_page(flow.venture_id, public_details(flow.venture_id)["name"],
            {"mode": "endpoint", "url": "https://owner-site.example/public/waitlists/" + flow.venture_id,
             "notice": "Your signups are saved on your laptop."})
        values["site_url"] = "https://owner-site.example"
    if mode == "mailto":
        values["waitlist_email"] = "updates@owner.example"
    flow.request("PUT", "/local/settings/publishing", json=values)
    flow.import_tool_page()
    destination = flow.request("GET", flow.path("/site/local")).json()["destination"]
    assert destination["mode"] == mode
    assert destination["notice"]
    zipped = flow.request("GET", flow.path("/site/local/export.zip"))
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        assert archive.read("index.html").decode() == flow.tool_html
        html = "\n".join(archive.read(name).decode() for name in archive.namelist() if name.endswith(".html"))
    if mode == "netlify":
        assert 'data-netlify="true"' not in html and 'netlify="' not in html
        assert "Netlify" in destination["notice"] and "import" in destination["notice"].lower()
    elif mode == "mailto":
        assert "mailto:updates@owner.example" not in html
        assert "not stored automatically" in destination["notice"] and "Collect signups" in destination["notice"]
    elif mode == "setup":
        assert "Settings > Keys and connections > Publishing" in destination["notice"]
        assert "save a Waitlist email or a reachable site URL" in destination["notice"]
        assert "return to Website" in destination["notice"]
    else:
        assert destination["url"].startswith("https://owner-site.example/public/waitlists/")
        assert destination["url"] in html, "Your exported waitlist must preserve your selected reachable endpoint."
        assert "form-action https://owner-site.example" in unescape(html)
        assert destination["url"] in flow.request("GET", flow.path("/site/local/export.html")).text
    assert not flow.external.public_files


def test_public_waitlist_requires_consent_deduplicates_and_respects_close(page_flow):
    """Visitors consent once; closing your form stops new signups, never emails."""
    flow = page_flow
    flow.request("PUT", flow.path("/waitlist"), json={"enabled": True})
    path = f"/public/waitlists/{flow.venture_id}"
    form = flow.request("GET", path, status=404).text
    assert "<form" not in form and "Coding tools" in form
    from app.conversion import waitlist
    details = waitlist.public_details(flow.venture_id)
    hidden = {"consent_version": details["consent_version"],
              "consent_digest": hashlib.sha256(details["consent"].encode()).hexdigest()}
    payload = {**hidden, "email": "visitor@audience.example", "consent": False}
    flow.request("POST", path, json=payload, status=422)
    payload["consent"] = True
    for attempt in range(2):
        flow.request("POST", path, json=payload, status=201)
    saved = flow.request("GET", flow.path("/waitlist")).json()
    assert saved["total"] == 1
    flow.request("PUT", flow.path("/waitlist"), json={"enabled": False})
    flow.request("POST", path, json={**payload, "email": "another@audience.example"}, status=404)
    assert flow.request("GET", flow.path("/waitlist")).json()["total"] == 1


def test_imported_waitlist_requires_consent_and_is_idempotent(page_flow):
    """Importing signups requires consent and never guesses a visitor address."""
    flow = page_flow
    flow.request("PUT", flow.path("/waitlist"), json={"enabled": True})
    path = flow.path("/site/local/signups")
    flow.request("POST", path, json={"emails": ["reader@audience.example"], "consent": False}, status=422)
    first = flow.request("POST", path, json={"emails": ["reader@audience.example"], "consent": True}).json()
    assert first["imported"] == 1
    repeated = flow.request("POST", path, json={"emails": ["reader@audience.example"], "consent": True}).json()
    assert repeated["imported"] == 0
    assert flow.request("GET", flow.path("/waitlist")).json()["total"] == 1


def test_waitlist_invitation_draft_keeps_source_optout_and_sender_address(page_flow):
    """Your requested invitation is a draft with disclosure and opt-out, not a send."""
    flow = page_flow
    address = "visitor@audience.example"
    contact_id = flow.import_contact(address)
    flow.secrets.append("owner-mail-password-sentinel")
    flow.request("PUT", "/local/settings/smtp", json={"host": "smtp.owner.example", "port": 587,
        "user": "you@owner.example", "password": flow.secrets[-1], "sender_name": "Your venture",
        "postal_address": "42 Orchard Road, Portland, OR 97201"})
    draft = flow.request("POST", "/api/v1/outreach/drafts", json={"contact_id": contact_id, "kind": "email"}).json()
    body = draft["body"]
    assert "I have your address from my own contacts." in body
    assert "no thanks" in body.lower() or "reply stop" in body.lower()
    assert "42 Orchard Road, Portland, OR 97201" in body
    assert not draft.get("sent_at") and not flow.external.mail


def test_reimporting_a_signup_cannot_clear_your_visitor_optout(page_flow):
    """An opt-out still blocks drafts after the same visitor is imported again."""
    flow = page_flow
    address = "visitor@audience.example"
    contact_id = flow.import_contact(address)
    flow.request("POST", "/api/v1" + flow.path(f"/gtm/journey/prospects/{contact_id}/reply"),
                 json={"text": "Please stop emailing me.", "outcome": "opted_out"})
    flow.request("PUT", flow.path("/waitlist"), json={"enabled": True})
    flow.request("POST", flow.path("/site/local/signups"), json={"emails": [address], "consent": True})
    assert flow.import_contact(address) == contact_id
    rejected = flow.request("POST", "/api/v1/outreach/drafts", json={"contact_id": contact_id, "kind": "email"}, status=409)
    assert "suppressed" in rejected.text.lower() or "opt" in rejected.text.lower()
    assert not flow.external.mail


@pytest.mark.parametrize("cap,drafts", [(0, ["draft-owner"]), (6, ["draft-owner"]), (5, ["draft-owner"] * 6)])
def test_waitlist_outreach_cannot_bypass_send_caps(page_flow, cap, drafts):
    """Invalid send caps fail before any page invitation reaches your mail server."""
    flow = page_flow
    flow.request("POST", "/api/v1" + flow.path("/gtm/journey/send"), status=422,
        json={"batch_id": "owner-batch", "draft_ids": drafts, "approval_digest": hashlib.sha256(b"unapproved").hexdigest(), "cap": cap})
    flow.drain()
    assert not flow.external.mail


def test_manual_counts_save_locally_and_correct_without_double_counting(page_flow):
    """Your manual counts are private, correctable, and usable without a server."""
    flow = page_flow
    app_id = flow.app()
    payload = flow.counts()
    reply = flow.chat("Record page counts: " + json.dumps(payload))
    assert "saved on your laptop" in reply["body_md"]
    payload["views"] = 135
    flow.request("POST", flow.path("/measurement/counts"), json=payload)
    from sqlalchemy import select
    from app.db.supymem_models import AppEvent
    from app.state import db

    with db()() as session:
        events = list(session.scalars(select(AppEvent).where(AppEvent.app_id == app_id, AppEvent.name == "manual_counts")))
        assert len(events) == 1
        assert json.loads(events[0].props)["views"] == 135
    assert not flow.external.requests


@pytest.mark.parametrize("existing_app", [False, True], ids=["new-target", "existing-target"])
def test_your_built_page_accepts_counts_without_a_separate_app_setup(page_flow, existing_app):
    """After building your page, manual measurement needs no hidden app record."""
    flow = page_flow
    if existing_app:
        flow.app()
    flow.import_tool_page()
    saved = flow.request("POST", flow.path("/measurement/counts"), json=flow.counts()).json()
    assert saved["counts"] == {"views": 120, "clicks": 18, "signups": 6}
    assert "saved on your laptop" in saved["body_md"]
    flow.import_tool_page()
    flow.request("POST", flow.path("/measurement/counts"), json=flow.counts(views=135))
    from sqlalchemy import select
    from app.db.supymem_models import AppEvent, FounderApp
    from app.state import db

    with db()() as session:
        apps = list(session.scalars(select(FounderApp).where(FounderApp.venture_id == flow.venture_id)))
        assert len(apps) == 1 and apps[0].workspace_id == flow.workspace_id
        events = list(session.scalars(select(AppEvent).where(AppEvent.app_id == apps[0].id, AppEvent.name == "manual_counts")))
        assert len(events) == 1 and json.loads(events[0].props)["views"] == 135


@pytest.mark.parametrize("changes,next_step", [
    ({"views": -1}, "non-negative whole numbers"),
    ({"views": True}, "non-negative whole numbers"),
    ({"end_date": "2099-01-01"}, "past or current date range"),
    ({"path": "/?email=visitor@audience.example"}, "without a query string or personal information"),
])
def test_invalid_manual_counts_name_a_correction_and_store_nothing(page_flow, changes, next_step):
    """Your count corrections validate dates, numbers, and private page paths."""
    flow = page_flow
    app_id = flow.app()
    rejected = flow.request("POST", flow.path("/measurement/counts"), json=flow.counts(**changes), status=422)
    assert next_step in rejected.json()["detail"]
    from sqlalchemy import select
    from app.db.supymem_models import AppEvent
    from app.state import db

    with db()() as session:
        assert not list(session.scalars(select(AppEvent).where(AppEvent.app_id == app_id, AppEvent.name == "manual_counts")))


def test_local_events_accept_anonymous_views_reject_personal_data_and_honor_off(page_flow):
    """Local page events never store visitor identities or bypass your switch."""
    flow = page_flow
    app_id = flow.app()
    snippet = flow.request("POST", f"/apps/{app_id}/ingest-key", status=201).json()
    key = snippet["secret"]
    event = {"key": key, "event": "page_view", "props": {"path": "/"}}
    assert flow.request("POST", "/e", json=event).json() == {"ok": True}
    flow.request("POST", "/e", json={**event, "props": {"email": "visitor@audience.example"}}, status=204)
    flow.switch("page_measurement", False)
    flow.request("POST", "/e", json=event, status=204)
    from sqlalchemy import select
    from app.db.supymem_models import AppEvent
    from app.state import db

    with db()() as session:
        events = list(session.scalars(select(AppEvent).where(AppEvent.app_id == app_id, AppEvent.name == "page_view")))
        assert len(events) == 1 and json.loads(events[0].props) == {"path": "/"}
    assert not flow.external.requests
