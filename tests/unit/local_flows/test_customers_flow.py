"""Owner HTTP journeys, with replay models and fake external transports only."""

import asyncio
import imaplib
import json
import os
import re
import smtplib
import socket
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from tests.unit.test_local_mode import _owner_client, local_owner_app


@pytest.fixture(autouse=True)
def product_checkout(monkeypatch):
    """Allow a read-only integration app while its product lanes reconcile."""
    monkeypatch.delenv("NEW_USER_FLOW", raising=False)
    root = os.environ.get("LOCAL_FLOW_APP_ROOT")
    if root:
        from tests import company_os_harness

        monkeypatch.setattr(company_os_harness, "COMPANY_OS", Path(root) / "services/company_os")


@pytest.fixture(params=[
    pytest.param({
        "idea": "A maintenance scheduler for independent marina operators",
        "audience": "Independent marina operators managing boat maintenance",
        "problem": "Marina operators lose repair requests across paper maintenance logs",
        "basis": "legitimate_interest",
        "mode": "company",
    }, id="b2b"),
    pytest.param({
        "idea": "A knitting project planner for hobby knitters",
        "audience": "Individual hobbyists tracking unfinished knitting projects at home",
        "problem": "Hobby knitters forget stitch counts between unfinished projects",
        "basis": "consent",
        "mode": "consumer",
    }, id="consumer"),
])
def customer_case(request):
    identity = uuid4().hex[:12]
    return {**request.param, "host": f"source-{identity}.test", "name": f"Robin {identity}",
        "email": f"robin@source-{identity}.test"}


@pytest.fixture
def model_configured(request):
    return getattr(request, "param", True)


@pytest.fixture
def smtp_password_configured(request):
    return getattr(request, "param", True)


class CustomerFlow:
    def __init__(self, client, case, external):
        self.client, self.case, self.external = client, case, external
        self.responses = []
        self.mail_configured = False
        idea = (f"Making: {case['idea']}\nAudience: {case['audience']}\nProblem: {case['problem']}\n"
            "Progress: I have not launched and have no customers or revenue yet.")
        venture = self.request("POST", "/ventures", {"raw_idea": idea, "start": False}).json()
        self.venture_id, self.workspace_id = venture["id"], venture["workspace_id"]
        client.headers["X-Workspace-Id"] = self.workspace_id
        self.path = f"/ventures/{self.venture_id}"
        self.journey = "/api/v1" + self.path + "/gtm/journey"

    def request(self, method, path, body=None, *, status=None):
        response = self.client.request(method, path, json=body)
        self.responses.append(response.text)
        if status is None:
            assert response.status_code < 400, response.text
        else:
            assert response.status_code == status, response.text
        return response

    def run_once(self):
        from app.worker.main import run_worker

        async def once():
            await run_worker(once=True, stop=asyncio.Event())

        self.client.portal.call(once)

    def drain(self):
        from sqlalchemy import or_, select
        from app.core.jobs import CLAIMABLE
        from app.db.models import Job
        from app.state import db
        from app.worker.catalog import enabled_kinds
        from core.clock import utcnow

        for attempt in range(12):
            with db()() as session:
                now = utcnow()
                pending = list(session.scalars(select(Job).where(Job.venture_id == self.venture_id,
                    Job.kind.in_(enabled_kinds()), Job.status.in_(CLAIMABLE), Job.cancel_requested.is_(False),
                    or_(Job.run_after.is_(None), Job.run_after <= now),
                    or_(Job.scheduled_for.is_(None), Job.scheduled_for <= now))))
            if not pending:
                return
            self.run_once()
        pytest.fail("Your requested jobs did not drain through the local worker.")

    def chat(self, message):
        reply = self.request("POST", self.path + "/chat",
            {"message": message, "idempotency_key": uuid4().hex}).json()
        self.drain()
        return reply

    def contacts(self):
        return self.request("GET", self.path + "/gtm/contacts").json()["items"]

    def job_errors(self):
        from sqlalchemy import select
        from app.db.models import Job
        from app.state import db

        with db()() as session:
            return [(job.kind, job.status, job.last_error) for job in session.scalars(
                select(Job).where(Job.venture_id == self.venture_id)) if job.last_error]

    def view(self):
        return self.request("GET", self.journey).json()

    def switch(self, name, on):
        return self.request("PUT", "/local/capabilities/" + name, {"on": on}).json()

    def configure_mail(self):
        if self.mail_configured:
            return
        values = {"host": "smtp.source.test", "port": 587,
            "user": "owner@source.test", "sender_name": "Your research team",
            "postal_address": "123 Test Street, Portland, OR 97201", "imap_host": "imap.source.test"}
        if self.smtp_password_configured:
            values["password"] = self.secrets[0]
        self.request("PUT", "/local/settings/smtp", values)
        self.request("GET", "/api/v1/ai-gtm/contacts")
        self.mail_configured = True

    def import_contact(self, *, email=None, basis=None):
        self.configure_mail()
        address = email or self.case["email"]
        preview = self.request("POST", self.path + "/gtm/contacts/import/preview", {
            "format": "csv", "text": f"name,email,country\n{self.case['name']},{address},US\n",
        }).json()
        result = self.request("POST", self.path + "/gtm/contacts/import", {
            "preview_id": preview["preview_id"], "lawful_basis_attested": True,
            "lawful_basis": basis or self.case["basis"],
            "lawful_basis_note": "You recorded permission during a voluntary research signup.",
        }).json()
        assert result["contact_ids"], result
        return result["contact_ids"][0]

    def draft(self):
        self.configure_mail()
        assert self.view()["icp"]["mode"] == self.case["mode"]
        reply = self.chat(f"Draft an email to {self.case['email']}. Save a draft only. Do not send anything.")
        assert_requested_reply(reply)
        view = self.view()
        assert view["icp"]["mode"] == self.case["mode"]
        drafts = [draft for draft in view["drafts"] if draft["to"] == self.case["email"]]
        assert drafts, "Your requested email draft must be saved without sending."
        return drafts[-1]

    def send(self):
        reply = self.chat(f"Send the email to {self.case['email']} now")
        assert_requested_reply(reply)
        return reply

    def assert_unsent(self):
        from sqlalchemy import select
        from app.db.models import GtmTouch
        from app.state import db

        assert not self.external.sent
        assert not self.external.published
        with db()() as session:
            assert not list(session.scalars(select(GtmTouch).where(
                GtmTouch.venture_id == self.venture_id, GtmTouch.direction == "out",
                GtmTouch.simulated == 0, GtmTouch.sent_at.is_not(None))))


@pytest.fixture
def flow(local_owner_app, monkeypatch, customer_case, model_configured, smtp_password_configured, caplog):
    caplog.set_level("DEBUG")
    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "0")
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    external = SimpleNamespace(requests=[], sent=[], published=[], mx=True)
    source_url = "https://" + customer_case["host"] + ("/forum" if customer_case["mode"] == "consumer" else "/contact")
    page = (f"<html><title>{customer_case['name']}</title><body><h1>{customer_case['audience']}</h1>"
        f"<p>{customer_case['problem']}. Contact {customer_case['name']} for a research interview.</p>"
        "<p>Community rules: Ask moderators for permission before recruiting research participants. "
        "No unsolicited advertisements or messages.</p>"
        f"<a href='mailto:{customer_case['email']}'>{customer_case['email']}</a></body></html>")

    def record(method, host, path):
        host = host.lower().rstrip(".")
        external.requests.append((method, host, path))
        assert host != "supymem.com" and not host.endswith(".supymem.com"), "Your laptop must not call Supymem hosts."

    def response(request):
        host = request.headers.get("host", request.url.host).split(":")[0]
        record(request.method, host, request.url.path)
        if host == "cloudflare-dns.com":
            data = ({"Status": 0, "Answer": [{"type": 15, "data": "10 mx.source.test"}]}
                if external.mx is True else {"Status": 3 if external.mx is False else 2})
            return httpx.Response(200, json=data, request=request)
        if host in {"api.tavily.com", "api.exa.ai"}:
            return httpx.Response(200, json={"results": [{"url": source_url,
                "title": customer_case["name"], "content": customer_case["problem"], "text": page}]}, request=request)
        if host in {"api.vercel.com", "api.netlify.com"} or host.endswith(".amazonaws.com"):
            external.published.append((request.method, host, request.url.path))
            return httpx.Response(200, json={"id": "fake-publication", "url": source_url}, request=request)
        if host == customer_case["host"] or host == "93.184.216.34":
            text = "User-agent: *\nAllow: /\n" if request.url.path == "/robots.txt" else page
            return httpx.Response(200, headers={"content-type": "text/html"},
                stream=httpx.ByteStream(text.encode()), request=request)
        if host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": []}, request=request)
        raise AssertionError(f"Your flow attempted unexpected external HTTP: {request.method} {host}{request.url.path}")

    def sync_transport(transport, request):
        return response(request)

    async def async_transport(transport, request):
        return response(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", sync_transport)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_transport)

    from ddgs.http_client import HttpClient, Response

    def search_transport(client, method, url, **options):
        record(method, urlsplit(url).hostname, urlsplit(url).path)
        markup = (f"<html><body><div class='body'><h2>{customer_case['name']}</h2>"
            f"<a href='{source_url}'>{customer_case['problem']}</a></div>"
            f"<ol id='b_results'><li class='b_algo'><h2><a href='{source_url}'>{customer_case['name']}</a></h2>"
            f"<div class='b_caption'><p>{customer_case['problem']}</p></div></li></ol></body></html>")
        return Response(SimpleNamespace(status_code=200, content=markup.encode(), text=markup,
            text_plain=customer_case["problem"], text_markdown=customer_case["problem"]))

    monkeypatch.setattr(HttpClient, "request", search_transport)

    def resolve(host, port, family=0, type=0, proto=0, flags=0):
        hostname = (host.decode() if isinstance(host, bytes) else str(host)).lower().rstrip(".")
        if hostname == "supymem.com" or hostname.endswith(".supymem.com"):
            record("DNS", hostname, "")
        address = "127.0.0.1" if host in {"localhost", "127.0.0.1"} else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port or 443))]

    def no_socket(connection, address):
        raise AssertionError("Your test attempted an unfaked network connection.")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", no_socket)

    class MailTransport:
        def __init__(self, host, port, **options):
            record("SMTP", host, str(port))

        def __enter__(self):
            return self

        def __exit__(self, *error):
            return False

        def ehlo(self):
            return 250, b"OK"

        def starttls(self, **options):
            return 220, b"OK"

        def login(self, user, password):
            assert password == fixture.secrets[0], "Your SMTP adapter must use your saved mailbox password."
            return 235, b"OK"

        def send_message(self, message, **options):
            external.sent.append(message)
            return {}

    monkeypatch.setattr(smtplib, "SMTP", MailTransport)
    monkeypatch.setattr(smtplib, "SMTP_SSL", MailTransport)

    class InboxTransport:
        def __init__(self, host, port, **options):
            record("IMAP", host, str(port))

        def __enter__(self):
            return self

        def __exit__(self, *error):
            return False

        def login(self, user, password):
            return "OK", [b"authenticated"]

        def select(self, mailbox="INBOX", readonly=True):
            return "OK", [b"0"]

        def response(self, name):
            return name, [b"1"]

        def uid(self, operation, *arguments):
            return "OK", [b""]

        def logout(self):
            return "BYE", [b"closed"]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", InboxTransport)
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        fixture = CustomerFlow(client, customer_case, external)
        fixture.secrets = ("fake-mail-" + uuid4().hex, "fake-publish-" + uuid4().hex)
        fixture.smtp_password_configured = smtp_password_configured
        if model_configured:
            fixture.request("PUT", "/local/providers", {"provider": "openai_compatible",
                "base_url": "http://127.0.0.1:11434/v1", "models": {"chat": "replay-fixture"}})
        fixture.request("PUT", "/local/settings/publishing", {"provider": "netlify", "project": "local-research",
            "token": fixture.secrets[1]})
        fixture.switch("send_email", False)
        fixture.switch("publish_pages", True)
        external.requests.clear()
        yield fixture
        assert all(host != "supymem.com" and not host.endswith(".supymem.com") for method, host, path in external.requests)
        assert all(secret not in text for secret in fixture.secrets for text in fixture.responses)
        assert all(secret not in caplog.text for secret in fixture.secrets)


def assert_requested_reply(reply):
    assert reply.get("kind") != "confirmation", "Your explicit request must run without a confirmation card."
    assert not any(action.get("confirmation") or action.get("type") == "confirmation" for action in reply.get("actions", []))
    assert not re.search(r"\b\d[\d,.]*\s+credits?\b|\bcredits?\s*[:=]\s*\d", reply.get("body_md", ""), re.I)
    assert not re.search(r'"(?:credits|credit_count|estimated_credits|credits_needed)"\s*:\s*\d', json.dumps(reply))


def test_opening_your_lead_list_never_starts_unrequested_work(flow):
    """You can open your venture without searching, mailing or publishing."""
    assert flow.contacts() == []
    assert flow.view()["icp"]["mode"] == flow.case["mode"]
    flow.run_once()
    flow.drain()
    assert not flow.external.requests
    flow.assert_unsent()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="CUSTOMERS-001")
def test_requested_public_search_keeps_real_source_evidence_on_your_lead_list(flow, caplog, record_testsuite_property):
    """Built-in keyless search and reader work for businesses and consumers."""
    flow.request("PATCH", flow.journey + "/icp", {"who": flow.case["audience"],
        "problem": flow.case["problem"], "recruitment_opt_in": True})
    flow.drain()
    reply = flow.chat("Find potential customers for my saved idea")
    assert_requested_reply(reply)
    record_testsuite_property("customer_discovery_worker_errors", json.dumps([
        {"kind": record.kind, "code": record.error_code, "traceback": record.traceback}
        for record in caplog.records if record.name == "app.worker" and hasattr(record, "error_code")]))
    view = flow.view()
    assert view["icp"]["mode"] == flow.case["mode"]
    leads = flow.contacts() if flow.case["mode"] == "company" else view["channels"] + view["need_signals"]
    assert leads, ("Your public customer search must save reachable leads, not leave an empty list.", flow.job_errors())
    assert any(host == flow.case["host"] for method, host, path in flow.external.requests)
    assert any(item.get("source_url", "").startswith("https://" + flow.case["host"]) for item in leads)
    assert all("Sample" not in json.dumps(item) for item in leads)
    flow.assert_unsent()


def test_requested_customer_search_has_no_confirmation_or_credit_count(flow):
    """You asked for the search, so no extra consent or credit card is shown."""
    reply = flow.chat("Find potential customers for my saved idea")
    assert_requested_reply(reply)
    flow.assert_unsent()


@pytest.mark.parametrize("capability_name", ["find_customers", "research"])
def test_search_switch_off_names_the_exact_screen_and_switch(flow, capability_name):
    """You get the precise switch to enable before any search is attempted."""
    blocked = flow.switch(capability_name, False)
    reply = flow.chat("Find potential customers for my saved idea")
    assert blocked["state"] == "off"
    assert blocked["next_step"] in reply["body_md"]
    assert "Settings > Capabilities" in reply["body_md"] and blocked["title"] in reply["body_md"]
    assert not flow.external.requests
    flow.assert_unsent()


@pytest.mark.parametrize("provider,missing", [("tavily", "API key"), ("exa", "API key"),
    ("firecrawl", "Firecrawl URL"), ("openai", "Model service (OpenAI or Azure OpenAI)")])
def test_missing_search_upgrade_names_the_key_or_setting_and_screen(flow, provider, missing):
    """An optional search upgrade without credentials has an actionable reply."""
    flow.request("PUT", "/local/settings/search", {"provider": provider})
    blocked = next(item for item in flow.request("GET", "/local/capabilities").json()["capabilities"]
        if item["name"] == "find_customers")
    assert blocked["state"] == "needs_setup"
    reply = flow.chat("Find potential customers for my saved idea")
    assert blocked["next_step"] in reply["body_md"]
    assert missing in reply["body_md"] and "Keys and connections" in reply["body_md"]
    assert not flow.external.requests
    flow.assert_unsent()


@pytest.mark.parametrize("model_configured", [False], indirect=True)
def test_missing_model_names_your_ai_provider_setup_screen(flow):
    """Without a saved model, discovery names your provider and chat-model setup screen."""
    blocked = next(item for item in flow.request("GET", "/local/capabilities").json()["capabilities"]
        if item["name"] == "find_customers")
    assert blocked["state"] == "needs_setup"
    reply = flow.chat("Find potential customers for my saved idea")
    assert blocked["next_step"] in reply["body_md"]
    assert "Keys and connections > Your AI provider" in reply["body_md"]
    assert "Chat model" in reply["body_md"]
    assert not flow.external.requests
    flow.assert_unsent()


def test_worker_rechecks_your_search_switch_after_a_job_was_requested(flow):
    """Turning discovery off before execution prevents queued external work."""
    requested = flow.request("PATCH", flow.journey + "/icp", {"who": flow.case["audience"],
        "problem": flow.case["problem"], "recruitment_opt_in": True}).json()
    assert requested.get("job_id"), "Your requested discovery must enter the real worker queue."
    blocked = flow.switch("find_customers", False)
    flow.drain()
    assert not flow.external.requests
    assert flow.contacts() == []
    reply = flow.chat("Show the customers you found")
    assert blocked["next_step"] in reply["body_md"]
    flow.assert_unsent()


def test_import_requires_your_lawful_basis_and_is_idempotent(flow):
    """Preview never imports; you attest permission before deduplicated commit."""
    path = flow.path + "/gtm/contacts/import"
    preview = flow.request("POST", path + "/preview", {"format": "paste", "text": flow.case["email"]}).json()
    assert preview["valid_rows"] == 1
    assert flow.contacts() == []
    denied = flow.request("POST", path, {"preview_id": preview["preview_id"],
        "lawful_basis_attested": False}, status=422).json()
    assert "lawful basis" in json.dumps(denied).lower()
    assert flow.contacts() == []
    invalid = flow.request("POST", path, {"preview_id": preview["preview_id"],
        "lawful_basis_attested": True, "lawful_basis": "guessed"}, status=422)
    assert invalid.status_code == 422 and flow.contacts() == []
    body = {"preview_id": preview["preview_id"], "lawful_basis_attested": True,
        "lawful_basis": flow.case["basis"], "lawful_basis_note": "You recorded the contact's permission."}
    first = flow.request("POST", path, body).json()
    repeated = flow.request("POST", path, body).json()
    assert first["contact_ids"] == repeated["contact_ids"]
    leads = flow.contacts()
    assert len(leads) == 1 and leads[0]["saved"]["lawful_basis"] == flow.case["basis"]
    assert leads[0]["saved"]["evidence"]["founder_import"]["lawful_basis_attested"] is True
    flow.drain()
    flow.assert_unsent()


def test_import_permission_failure_names_where_you_can_record_permission(flow):
    """Missing consent tells you to record lawful basis on the contact import screen."""
    path = flow.path + "/gtm/contacts/import"
    preview = flow.request("POST", path + "/preview", {"format": "paste", "text": flow.case["email"]}).json()
    denied = flow.request("POST", path, {"preview_id": preview["preview_id"]}, status=422)
    message = json.dumps(denied.json()).lower()
    assert "find customers" in message and "import your existing contacts" in message
    assert "lawful basis" in message and ("record" in message or "select" in message)
    assert "basis for contacting" in message and "evidence of permission" in message
    assert flow.contacts() == []
    flow.assert_unsent()


def test_importing_a_sourced_contact_keeps_the_original_public_source(flow):
    """Your later permission attestation never deletes the original source URL."""
    source = "https://" + flow.case["host"] + "/contact"
    flow.request("POST", flow.journey + "/prospects", {"name": flow.case["name"],
        "email": flow.case["email"], "source_url": source, "country": "US",
        "lawful_basis": flow.case["basis"], "lawful_basis_note": "You recorded their research signup permission."})
    original = flow.contacts()[0]
    contact_id = flow.import_contact()
    leads = flow.contacts()
    assert len(leads) == 1 and leads[0]["id"] == contact_id == original["id"]
    assert leads[0]["source_url"] == source
    assert leads[0]["saved"]["lawful_basis"] == flow.case["basis"]
    evidence = leads[0]["saved"]["evidence"]
    assert all(evidence[key] == value for key, value in original["saved"]["evidence"].items()
        if key != "founder_import")
    assert leads[0]["saved"]["evidence"]["founder_import"]["lawful_basis_attested"] is True
    assert flow.import_contact() == contact_id
    flow.drain()
    flow.assert_unsent()


def test_requested_draft_keeps_disclosure_optout_and_sender_without_sending(flow):
    """Even with publishing enabled, a draft-only request sends and publishes nothing."""
    flow.import_contact()
    draft = flow.draft()
    assert "I have your address from my own contacts." in draft["body"]
    assert 'reply "no thanks"' in draft["body"]
    assert "123 Test Street, Portland, OR 97201" in draft["body"]
    assert draft["state"] == "draft" and not draft.get("sent_at")
    flow.assert_unsent()


def test_a_public_contact_draft_discloses_the_original_site(flow):
    """Your public-source draft names its actual source and keeps the opt-out footer."""
    source = "https://" + flow.case["host"] + "/contact"
    flow.request("POST", flow.journey + "/prospects", {"name": flow.case["name"],
        "email": flow.case["email"], "source_url": source, "country": "US",
        "lawful_basis": flow.case["basis"], "lawful_basis_note": "You recorded their research signup permission."})
    contact_id = flow.contacts()[0]["id"]
    flow.request("PATCH", flow.journey + f"/prospects/{contact_id}", {"country": "US",
        "lawful_basis": flow.case["basis"], "lawful_basis_note": "You recorded their research signup permission."})
    draft = flow.draft()
    assert f"Your email address is listed publicly on {flow.case['host']}." in draft["body"]
    assert 'reply "no thanks"' in draft["body"] and "123 Test Street" in draft["body"]
    flow.assert_unsent()


@pytest.mark.parametrize("blocked_state", ["off", "host", "user", "sender_name", "postal_address"])
def test_requested_send_blocked_state_names_the_exact_next_step(flow, blocked_state):
    """Send failures name the disabled feature or missing mailbox field and screen."""
    flow.import_contact()
    flow.draft()
    if blocked_state != "off":
        flow.switch("send_email", True)
        values = {blocked_state: ""}
        if blocked_state in {"host", "user"}:
            values["password"] = flow.secrets[0]
        flow.request("PUT", "/local/settings/smtp", values)
    blocked = next(item for item in flow.request("GET", "/local/capabilities").json()["capabilities"]
        if item["name"] == "send_email")
    reply = flow.send()
    assert blocked["next_step"] in reply["body_md"]
    flow.assert_unsent()


@pytest.mark.parametrize("smtp_password_configured", [False], indirect=True)
def test_requested_send_without_a_saved_password_names_the_exact_next_step(flow):
    """You can save other mailbox fields first; sending names the missing app password."""
    flow.import_contact()
    flow.switch("send_email", True)
    blocked = next(item for item in flow.request("GET", "/local/capabilities").json()["capabilities"]
        if item["name"] == "send_email")
    assert blocked["state"] == "needs_setup" and "Mail app password" in blocked["missing"]
    reply = flow.send()
    assert blocked["next_step"] in reply["body_md"]
    flow.assert_unsent()


def test_requested_send_uses_your_saved_mailbox_without_confirmation(flow):
    """Your explicit send reaches only your configured SMTP with the compliant draft."""
    flow.import_contact()
    draft = flow.draft()
    flow.switch("send_email", True)
    reply = flow.send()
    assert_requested_reply(reply)
    assert len(flow.external.sent) == 1, "Your enabled explicit send must reach your own SMTP mailbox."
    message = flow.external.sent[0]
    assert message["To"] == flow.case["email"] and "owner@source.test" in message["From"]
    assert "I have your address from my own contacts." in message.get_content()
    assert 'reply "no thanks"' in message.get_content() and "123 Test Street" in message.get_content()
    assert not flow.external.published
    saved = next(item for item in flow.view()["drafts"] if item["doc_id"] == draft["doc_id"])
    assert saved["state"] == "sent" and saved.get("sent_at")


def test_suppressed_contact_stays_blocked_after_reimport(flow):
    """A recorded opt-out survives reimport and blocks an explicitly requested resend."""
    contact_id = flow.import_contact()
    flow.draft()
    flow.switch("send_email", True)
    flow.send()
    assert len(flow.external.sent) == 1, "Your first requested send must finish before recording its reply."
    flow.request("POST", flow.journey + f"/prospects/{contact_id}/reply",
        {"text": "No thanks. Please do not email me again.", "outcome": "opted_out"})
    assert flow.import_contact() == contact_id
    lead = next(item for item in flow.contacts() if item["id"] == contact_id)
    assert lead["status"] == "opted out"
    reply = flow.send()
    assert len(flow.external.sent) == 1
    assert "another person" in reply["body_md"].lower() or "opted out" in reply["body_md"].lower()
    assert not flow.external.published


def test_your_send_cap_blocks_a_second_contact_without_extra_delivery(flow):
    """You can cap daily sends in the app; a further request cannot exceed that cap."""
    flow.import_contact()
    flow.draft()
    flow.switch("send_email", True)
    flow.request("PUT", flow.path + "/autopilot", {"allow_send": True, "send_max_per_day": 1,
        "confirmed_send_max": True})
    flow.send()
    assert len(flow.external.sent) == 1, "Your first explicit send must use the available daily slot."
    second_email = "other@" + flow.case["host"]
    flow.import_contact(email=second_email)
    flow.chat(f"Draft an email to {second_email}. Save a draft only. Do not send anything.")
    reply = flow.chat(f"Send the email to {second_email} now")
    assert len(flow.external.sent) == 1, "Your send cap must prevent any second SMTP delivery."
    assert "tomorrow" in reply["body_md"].lower() or "daily" in reply["body_md"].lower()
    assert not flow.external.published


def test_an_unverified_import_never_becomes_a_guessed_send(flow):
    """An unreachable email domain never earns send eligibility from your attestation."""
    flow.external.mx = None
    flow.import_contact()
    lead = flow.contacts()[0]
    assert lead["saved"]["evidence"]["founder_import"]["email_check"] == "unknown"
    assert not lead["can_draft"]
    flow.switch("send_email", True)
    flow.chat(f"Draft an email to {flow.case['email']}. Do not send anything.")
    reply = flow.send()
    assert not flow.external.sent
    assert "email" in reply["body_md"].lower()
    assert "Find customers > Import your existing contacts" in reply["body_md"]
    assert "Which basis" not in reply["body_md"]
    flow.assert_unsent()


def test_your_settings_and_lead_apis_never_return_saved_secrets(flow):
    """Owner APIs expose readiness, never mailbox or publishing credentials."""
    from app.config import data_root

    flow.import_contact()
    for path in ("/local/settings/smtp", "/local/settings/publishing", "/local/settings/catalog",
        "/local/providers", "/local/capabilities", flow.path + "/gtm/contacts", flow.journey):
        response = flow.request("GET", path)
        assert all(secret not in response.text for secret in flow.secrets)
    for path in data_root().rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert all(secret.encode() not in content for secret in flow.secrets), "Your saved credentials must stay encrypted on disk."
    flow.assert_unsent()
