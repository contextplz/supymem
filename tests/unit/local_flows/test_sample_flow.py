"""Your keyless sample stays private, labelled, and usable until you choose live AI."""

import asyncio
import hashlib
import imaplib
import re
import smtplib
import socket
from html import escape
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import requests
from sqlalchemy import select

from tests.unit.test_local_acceptance import acceptance
from tests.unit.test_local_mode import _owner_client, local_owner_app


@pytest.fixture(autouse=True)
def local_chat():
    """Your first run keeps empty saved settings instead of the shared live-model fixture."""


@pytest.fixture(params=[
    pytest.param(("Scheduling software for independent veterinary clinics",
                  "Practice managers at independent veterinary clinics"), id="b2b"),
    pytest.param(("A meal planning journal for recreational trail runners",
                  "Recreational trail runners planning meals for weekend races"), id="consumer"),
])
def audience(request):
    return request.param


@pytest.fixture
def builder_page(audience):
    return ('<!doctype html><html lang="en"><body><main><h1>' + escape(audience[0])
            + '</h1><p>Sample page, not market evidence.</p></main></body></html>')


@pytest.fixture
def transports(monkeypatch):
    from openai import _base_client

    model_httpx = getattr(_base_client, "httpx2", httpx)
    calls = []
    authorizations = []

    def response(request):
        wire = httpx if isinstance(request, httpx.Request) else model_httpx
        calls.append(("http", request.method, request.url.host))
        authorizations.append(hashlib.sha256(request.headers.get("Authorization", "").encode()).hexdigest())
        if request.url.path.endswith("/chat/completions"):
            return wire.Response(200, request=request, json={"id": "offline-connection-test",
                "object": "chat.completion", "model": "gpt-6-luna",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"connected":true}'},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        return wire.Response(200, request=request, json={"Status": 3, "results": [], "data": []})

    async def async_response(transport, request):
        return response(request)

    for wire in {httpx, model_httpx}:
        monkeypatch.setattr(wire.HTTPTransport, "handle_request", lambda transport, request: response(request))
        monkeypatch.setattr(wire.AsyncHTTPTransport, "handle_async_request", async_response)

    def requests_response(session, request, **options):
        calls.append(("http", request.method, httpx.URL(request.url).host))
        reply = requests.Response()
        reply.status_code = 200
        reply._content = b'{"results": [], "data": []}'
        reply.request = request
        return reply

    monkeypatch.setattr(requests.Session, "send", requests_response)

    class MailTransport:
        def __init__(self, host, *args, **options):
            calls.append(("mail", "connect", host))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, **options):
            return 220, b"ready"

        def login(self, *args):
            return "OK", []

        def send_message(self, *args, **options):
            calls.append(("mail", "send", None))
            return {}

        def sendmail(self, *args, **options):
            calls.append(("mail", "send", None))
            return {}

        def select(self, *args, **options):
            return "OK", [b"0"]

        def search(self, *args):
            return "OK", [b""]

    for module, names in ((smtplib, ("SMTP", "SMTP_SSL")), (imaplib, ("IMAP4_SSL",))):
        for name in names:
            monkeypatch.setattr(module, name, MailTransport)

    original_connect = socket.socket.connect

    def connect(transport, address):
        if transport.family in {socket.AF_INET, socket.AF_INET6}:
            calls.append(("unfaked_socket", "connect", address[0]))
            raise AssertionError("Your test must not open a network connection.")
        return original_connect(transport, address)

    def resolve(*args, **options):
        calls.append(("dns", "resolve", args[0]))
        raise AssertionError("Your sample must not make a DNS request.")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return SimpleNamespace(calls=calls, authorizations=authorizations)


@pytest.fixture
def keyless_environment(monkeypatch, transports):
    for name in ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPATIBLE_API_KEY",
                 "SUPYMEM_LLM_API_KEY", "SUPYMEM_LLM_PROVIDER", "SUPYMEM_MODEL_CHAT", "OPENAI_BASE_URL",
                 "AZURE_OPENAI_ENDPOINT", "ANTHROPIC_BASE_URL", "OPENAI_COMPATIBLE_BASE_URL",
                 "TAVILY_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY", "FIRECRAWL_URL",
                 "SUPYMEM_SMTP_PASSWORD", "SMTP_PASSWORD", "SMTP_HOST", "SMTP_USERNAME", "IMAP_HOST",
                 "COMPOSIO_API_KEY", "VERCEL_TOKEN", "NETLIFY_AUTH_TOKEN", "AWS_ACCESS_KEY_ID",
                 "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def owner(monkeypatch, tmp_path, transports, acceptance, keyless_environment, local_owner_app):
    from app.config import settings
    from app.worker import main as worker

    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    monkeypatch.setenv("GUESSED_EMAILS", "off")
    monkeypatch.delenv("SUPYMEM_SINGLE_PROCESS", raising=False)
    monkeypatch.delenv("WORKER_JOB_CLASSES", raising=False)
    monkeypatch.setattr(settings, "new_user_flow", "on")
    monkeypatch.setattr(worker, "CONCURRENCY", 1)
    monkeypatch.setattr(worker, "INTERACTIVE_SLOTS", 1)
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        yield SimpleNamespace(http=client, calls=transports.calls, authorizations=transports.authorizations,
                              secrets=[], root=tmp_path)
    assert not any(isinstance(host, str) and (host == "supymem.com" or host.endswith(".supymem.com"))
                   for category, method, host in transports.calls), "Your local app must never contact a hosted product service."


def drain():
    from app.core.jobs import ACTIVE_JOB_STATUSES
    from app.db.models import Job
    from app.state import db
    from app.worker.main import _run_worker

    for attempt in range(30):
        with db()() as session:
            pending = list(session.scalars(select(Job).where(Job.status.in_(ACTIVE_JOB_STATUSES))))
            errors = list(session.scalars(select(Job).where(Job.last_error.is_not(None))))
        assert not errors, [(job.kind, job.last_error) for job in errors]
        if not pending:
            return
        asyncio.run(_run_worker(once=True, stop=asyncio.Event()))
    pytest.fail("Your requested work did not finish in the real local worker loop.")


def api(owner, method, path, payload=None, *, html=False, allow_error=False):
    reply = owner.http.request(method, path, json=payload)
    owner.last_response = reply
    assert all(secret not in reply.text for secret in owner.secrets), "Your saved secrets must not be returned by an API."
    if not allow_error:
        assert reply.status_code < 400, (path, reply.status_code, reply.text)
    drain()
    return reply.text if html else reply.json()


def no_confirmation_or_credit_count(reply):
    assert reply.get("kind") != "confirmation", "Your requested work must not ask for a confirmation card."
    assert not any(action.get("confirmation") for action in reply.get("actions", []))
    assert not re.search(r"\b\d+(?:[.,]\d+)?\s*credits?\b|\bcredits?\s*[:=]\s*\d",
                         reply.get("body_md", ""), re.I), "Your reply must not show credit amounts."


def chat(owner, journey, message):
    reply = api(owner, "POST", journey.path("/chat"), {"message": message, "idempotency_key": uuid4().hex})
    no_confirmation_or_credit_count(reply)
    return reply


def venture(owner, audience, acceptance, *, sample=True, confirm=False):
    class ProductClient:
        headers = owner.http.headers

        def request(self, method, path, payload=None, *, html=False):
            result = api(owner, method, path, payload, html=html, allow_error=True)
            if owner.last_response.status_code >= 400:
                raise acceptance.AcceptanceFailure("Your requested API step failed.", status=owner.last_response.status_code)
            return result

    before = api(owner, "GET", "/local/providers")
    assert not before.get("configured")
    if sample:
        api(owner, "POST", "/local/providers/sample", {})
        assert api(owner, "GET", "/setup/status")["llm_mode"] == "sample"
    journey = acceptance.Journey(ProductClient(), SimpleNamespace(stub=True, idea=audience[0], answer=audience[1],
        timeout=2, output=owner.root / "sample-page.html"))
    journey.create()
    if confirm:
        journey.answer()
        journey.skip()
        journey.confirm()
    return journey


def sample_drafts(owner, audience, acceptance):
    journey = venture(owner, audience, acceptance, confirm=True)
    journey.customers()
    journey.design()
    reply = chat(owner, journey, "Prepare email invitations for my saved customer interview experiment. Save drafts only. Do not send anything")
    status = api(owner, "GET", "/api/v1" + journey.path("/gtm/journey"))
    assert status["drafts"], reply.get("body_md")
    return journey, status


def test_your_first_run_stays_idle_until_you_request_work(owner, audience, acceptance):
    """Choosing your sample and saving an idea does not start research, send, or publish."""
    from app.db.models import GtmTouch, Job
    from app.state import db

    journey = venture(owner, audience, acceptance)
    for suffix in ("/onboarding", "/chat", "/research/dossiers", "/experiments"):
        api(owner, "GET", journey.path(suffix))
    with db()() as session:
        jobs = list(session.scalars(select(Job)))
        assert all(job.kind == "idea.clarify_turn" for job in jobs), [(job.kind, job.status) for job in jobs]
        assert not session.scalar(select(GtmTouch.id))
    assert owner.calls == []


def test_your_keyless_sample_offers_tool_setup_and_exports_only_the_supplied_page(owner, audience, acceptance, builder_page):
    """Your sample stays labelled; builds need your tool and exports use its supplied page."""
    from app.firstlook.service import ingest_builder_page

    journey, status = sample_drafts(owner, audience, acceptance)
    request = {"request_key": uuid4().hex}
    started = api(owner, "POST", journey.path("/research/dossier/regenerate"), request)
    repeated = api(owner, "POST", journey.path("/research/dossier/regenerate"), request)
    assert started["job_id"] == repeated["job_id"]
    dossiers = api(owner, "GET", journey.path("/research/dossiers"))["items"]
    assert dossiers and dossiers[0]["state"] in {"complete", "partial"}
    report = api(owner, "GET", journey.path("/research/dossiers/" + dossiers[0]["id"]))
    findings = [finding for group in report["findings"].values() for finding in group]
    assert findings and all(finding["evidence_kind"] == "sample" and not finding["page_verified"]
                            and "Sample" in finding["title"] and "Sample" in finding["source"] for finding in findings)
    assert all(httpx.URL(finding["url"]).host.endswith(".example.invalid") for finding in findings)
    preview = api(owner, "GET", journey.path("/research/dossier"))
    assert preview["available"] and preview["mode"] == "sample"
    assert "Sample research" in preview["markdown"]
    assert status["prospects"] and all("Sample" in person["name"] and "Sample" in person["evidence_label"]
        and not person["eligible"] and person["email"].endswith(".example.invalid") for person in status["prospects"])
    assert journey.experiments() and all(item["source"] == "Sample" and item["hypothesis"].startswith("Sample: ")
                                       for item in journey.experiments())
    for draft in status["drafts"]:
        assert "Sample" in draft["subject"] and "Sample" in draft["body"]
        assert "Your email address is listed publicly on " in draft["body"]
        assert 'reply "no thanks"' in draft["body"]
        assert status["sender_postal_address"] and status["sender_postal_address"] in draft["body"]
        assert draft["state"] == "draft" and not draft.get("sent_at")
    assert not status["can_send"] and status["results"]["sent"] == 0
    reply = chat(owner, journey, "Build a landing page for my saved idea. Keep it private and do not send anything")
    assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
    assert any(action.get("href") == "/settings/local#coding-tools" for action in reply["actions"])
    assert not journey.page() and not journey.args.output.exists()
    assert ingest_builder_page(journey.venture, builder_page)["ok"]
    journey.export()
    assert journey.args.output.is_file()
    assert "not market evidence" in journey.args.output.read_text()
    assert owner.calls == []


BLOCKED_ACTIONS = [
    pytest.param("research", "Research the market for my saved idea", id="research"),
    pytest.param("find_customers", "Find potential customers for my saved idea", id="find_customers"),
    pytest.param("landing_pages", "Build a landing page for my saved idea. Keep it private", id="landing_pages"),
    pytest.param("content", "Write a LinkedIn post for my saved idea. Save a draft only", id="content"),
    pytest.param("send_email", "Send my saved email invitations now", id="send_email"),
    pytest.param("read_replies", "Check my inbox for replies to my outreach", id="read_replies"),
    pytest.param("publish_pages", "Publish my saved landing page now", id="publish_pages"),
]


@pytest.mark.parametrize("name,message", BLOCKED_ACTIONS)
def test_your_switched_off_feature_names_the_exact_app_switch(owner, audience, acceptance, name, message):
    """Your real venture respects your off switch; sample email is blocked separately."""
    journey = venture(owner, audience, acceptance, sample=False)
    blocked = api(owner, "PUT", "/local/capabilities/" + name, {"on": False})
    assert blocked["state"] == "off"
    assert "Settings > Capabilities" in blocked["next_step"] and blocked["title"] in blocked["next_step"]
    reply = chat(owner, journey, message)
    assert blocked["next_step"] in reply["body_md"]
    assert owner.calls == []


@pytest.mark.parametrize("name,message", BLOCKED_ACTIONS)
def test_your_missing_setting_names_the_exact_key_and_screen(owner, audience, acceptance, name, message):
    """Missing optional service settings name your key or field, its screen, and what to retry."""
    journey = venture(owner, audience, acceptance, sample=False)
    if name in {"research", "find_customers"}:
        api(owner, "PUT", "/local/settings/search", {"provider": "tavily"})
    blocked = api(owner, "PUT", "/local/capabilities/" + name, {"on": True})
    assert blocked["state"] == "needs_setup" and blocked["missing"]
    assert "Keys and connections" in blocked["next_step"]
    if name == "landing_pages":
        assert "Settings > Keys and connections > Coding tools" in blocked["next_step"]
        assert "sign in with your own subscription" in blocked["next_step"]
        assert "Allow Charlie" in blocked["next_step"]
    elif name == "find_customers":
        assert "Your search and page reader" in blocked["next_step"]
        assert blocked["missing"][0] in blocked["next_step"]
    else:
        assert all(field in blocked["next_step"] for field in blocked["missing"])
    reply = chat(owner, journey, message)
    assert blocked["next_step"] in reply["body_md"]
    if name == "find_customers":
        api(owner, "PUT", "/local/settings/search", {"api_key": "offline-search-fixture"})
        next_blocker = api(owner, "PUT", "/local/capabilities/" + name, {"on": True})
        assert next_blocker["state"] == "needs_setup" and next_blocker["missing"]
        assert "Your AI provider" in next_blocker["next_step"]
        assert all(field in next_blocker["next_step"] for field in next_blocker["missing"])
    assert owner.calls == []


@pytest.mark.parametrize("name,message", [
    pytest.param("send_email", "Send my saved email invitations now", id="send_email"),
    pytest.param("publish_pages", "Publish my saved landing page now", id="publish_pages"),
    pytest.param("read_replies", "Check my inbox for replies to my outreach", id="read_replies"),
])
def test_your_sample_never_uses_configured_outbound_services(owner, audience, acceptance, builder_page, name, message):
    """Even an explicit request with a ready service cannot send, publish or read a real inbox in sample mode."""
    journey, status = sample_drafts(owner, audience, acceptance)
    if name == "publish_pages":
        from app.firstlook.service import ingest_builder_page

        assert ingest_builder_page(journey.venture, builder_page)["ok"]
        journey.export()
    secret = "fixture-" + uuid4().hex
    owner.secrets.append(secret)
    api(owner, "PUT", "/local/settings/smtp", {"host": "smtp.mail.example.invalid", "port": 587,
        "user": "you@mail.example.invalid", "password": secret, "sender_name": "Your sender",
        "postal_address": "Your registered mailing address", "imap_host": "imap.mail.example.invalid"})
    api(owner, "PUT", "/local/settings/publishing", {"provider": "netlify", "project": "private-fixture", "token": secret})
    ready = api(owner, "PUT", "/local/capabilities/" + name, {"on": True})
    assert ready["state"] == "ready"
    reply = chat(owner, journey, message)
    assert owner.calls == [], "Your sample must not use any real service transport."
    assert "sample" in reply["body_md"].lower()
    assert "Keys and connections" in reply["body_md"] and "AI" in reply["body_md"]
    if name == "read_replies":
        assert "API key" in reply["body_md"] and "model" in reply["body_md"]
    after = api(owner, "GET", "/api/v1" + journey.path("/gtm/journey"))
    assert after["results"]["sent"] == 0 and not after["can_send"]
    assert not any(draft.get("sent_at") for draft in after["drafts"])
    assert owner.calls == []


def test_saving_your_key_switches_to_live_without_sending_or_publishing(owner, audience, acceptance, monkeypatch, caplog):
    """Saving your AI key leaves sample mode, verifies that service, and keeps existing sample data ineligible."""
    journey, status = sample_drafts(owner, audience, acceptance)
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    secret = "fixture-" + uuid4().hex
    owner.secrets.append(secret)
    saved = api(owner, "PUT", "/local/providers", {"provider": "openai", "api_key": secret,
        "model": "gpt-6-luna"})
    assert saved["configured"] and not saved["sample_selected"]
    assert api(owner, "GET", "/setup/status")["llm_mode"] == "live"
    assert owner.calls == [], "Saving your key must not start unrequested provider work."
    tested = api(owner, "POST", "/local/providers/test", {"provider": "openai", "model": "gpt-6-luna"})
    assert tested["ok"]
    assert owner.calls and all(category == "http" and host == "api.openai.com" for category, method, host in owner.calls)
    authorization = hashlib.sha256(("Bearer " + secret).encode()).hexdigest()
    assert owner.authorizations and all(value == authorization for value in owner.authorizations), \
        "Your model request must use your saved key, not a fallback credential."
    api(owner, "GET", "/local/providers")
    after = api(owner, "GET", "/api/v1" + journey.path("/gtm/journey"))
    assert all(not person["eligible"] and "Sample" in person["evidence_label"] for person in after["prospects"])
    assert after["results"]["sent"] == 0
    assert all(secret.encode() not in path.read_bytes() for path in owner.root.rglob("*") if path.is_file()), \
        "Your key must not be written in plaintext."
    assert secret not in caplog.text, "Your key must not be logged."


@pytest.mark.parametrize("block", ["suppression", "opt_out"])
def test_your_sample_respects_suppression_and_opt_out(owner, audience, acceptance, block):
    """An opted-out or suppressed sample contact stays blocked when you ask for another draft."""
    journey, before = sample_drafts(owner, audience, acceptance)
    contact_id = before["prospects"][0]["id"]
    assert before["prospects"][0]["email"]
    if block == "suppression":
        api(owner, "PATCH", "/api/v1/ai-gtm/contacts/" + contact_id, {"do_not_contact": True})
    else:
        api(owner, "POST", "/api/v1" + journey.path("/gtm/journey/prospects/" + contact_id + "/reply"),
            {"text": "No thanks. Please stop emailing me.", "outcome": "opted_out"})
    blocked = api(owner, "GET", "/api/v1" + journey.path("/gtm/journey"))
    target = next(person for person in blocked["prospects"] if person["id"] == contact_id)
    assert not target["eligible"]
    assert target["email"] is None
    existing = {draft["doc_id"] for draft in blocked["drafts"]}
    chat(owner, journey, "Prepare another batch of email invitations for my saved interview experiment. Save drafts only. Do not send anything")
    after = api(owner, "GET", "/api/v1" + journey.path("/gtm/journey"))
    assert not any(draft["contact_id"] == contact_id and draft["doc_id"] not in existing for draft in after["drafts"])
    assert after["results"]["sent"] == 0 and owner.calls == []


def test_your_zero_send_cap_and_sample_recipients_cannot_be_bypassed(owner, audience, acceptance):
    """Your zero cap stays saved, and a direct send request never delivers a sample contact."""
    journey, before = sample_drafts(owner, audience, acceptance)
    api(owner, "PUT", journey.path("/autopilot"),
        {"allow_send": True, "send_max_per_day": 0, "confirmed_send_max": True})
    status = api(owner, "GET", "/api/v1" + journey.path("/gtm/journey"))
    assert status["daily_cap"] == 0 and not status["can_send"]
    batch = status["batches"][0]
    reply = api(owner, "POST", "/api/v1" + journey.path("/gtm/journey/send"),
        {"batch_id": batch["batch_id"], "draft_ids": [before["drafts"][0]["doc_id"]],
         "approval_digest": batch["approval_digest"], "cap": 1}, allow_error=True)
    assert owner.last_response.status_code == 409
    assert isinstance(reply, dict)
    after = api(owner, "GET", "/api/v1" + journey.path("/gtm/journey"))
    assert after["daily_cap"] == 0 and after["results"]["sent"] == 0 and owner.calls == []
    assert "Keys and connections" in str(reply) and "AI" in str(reply)
