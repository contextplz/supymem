"""Owner-driven local mail flows. Only network transports and time are simulated."""

import asyncio
import imaplib
import json
import re
import secrets
import socket
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import httpx
import pytest
from sqlalchemy import create_engine, select

from tests.unit.test_local_mode import _owner_client, local_owner_app


@pytest.fixture(autouse=True)
def local_test_environment(monkeypatch):
    """Restore the shipped setup default overridden by the legacy suite's conftest."""
    monkeypatch.setenv("NEW_USER_FLOW", "on")
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.delenv("SUPYMEM_SINGLE_PROCESS", raising=False)


@pytest.fixture(params=[
    pytest.param({"idea": "A scheduling tool for independent machine repair shops",
                  "audience": "Owners of independent machine repair shops",
                  "problem": "Repair jobs lose time when parts and technicians arrive separately",
                  "domain": "workshop.example", "name": "Morgan"}, id="b2b"),
    pytest.param({"idea": "A home bread journal for adults learning sourdough baking",
                  "audience": "Adults learning to bake sourdough bread at home",
                  "problem": "Home bakers cannot tell which changes caused a failed loaf",
                  "domain": "bread.example", "name": "Taylor"}, id="consumer"),
])
def audience(request):
    return request.param


@dataclass
class MailTransport:
    password: str = field(default_factory=lambda: secrets.token_hex(24), repr=False)
    sent: list = field(default_factory=list)
    inbox: list = field(default_factory=list)
    hosts: list = field(default_factory=list)
    logins: list = field(default_factory=list, repr=False)
    reads: list = field(default_factory=list)
    tls: list = field(default_factory=list)
    fail_login: bool = False

    def reply(self, address, text, root):
        from core.clock import utcnow

        message = EmailMessage()
        message["From"] = address
        message["To"] = "owner@sender.example"
        message["Subject"] = "Re: your invitation"
        message["Message-ID"] = f"<reply-{len(self.inbox) + 1}@{address.split('@')[1]}>"
        message["In-Reply-To"] = root
        message["References"] = root
        message["Date"] = utcnow()
        message.set_content(text)
        self.inbox.append(message)

    def http(self, request):
        host = request.url.host
        self.hosts.append(host)
        if host == "supymem.com" or host.endswith(".supymem.com"):
            pytest.fail("Local mail must never request a Supymem host.")
        if host in {"dns.google", "cloudflare-dns.com"}:
            return httpx.Response(200, json={"Status": 0, "Answer": [
                {"type": 15, "data": "10 mx.example."}]})
        pytest.fail(f"Unexpected external HTTP request to {host}; add a transport fixture, not a product mock.")

    def install(self, monkeypatch):
        transport = self

        class SMTP:
            def __init__(self, host, port, **options):
                transport.hosts.append(host)
                assert host == "smtp.sender.example" and port in {465, 587}
                if port == 465:
                    assert options.get("context") is not None
                    transport.tls.append("smtp")

            def __enter__(self):
                return self

            def __exit__(self, *error):
                return False

            def starttls(self, *, context):
                assert context is not None
                transport.tls.append("smtp")

            def login(self, username, password):
                if transport.fail_login:
                    raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")
                transport.logins.append(("smtp", username, password))

            def send_message(self, message, *, from_addr, to_addrs):
                assert from_addr == "owner@sender.example" and to_addrs == [str(message["To"])]
                transport.sent.append(BytesParser(policy=policy.default).parsebytes(message.as_bytes()))
                return {}

        class IMAP:
            def __init__(self, host, port, **options):
                transport.hosts.append(host)
                assert host == "imap.sender.example" and port == 993
                assert options.get("ssl_context") is not None
                transport.tls.append("imap")

            def __enter__(self):
                return self

            def __exit__(self, *error):
                return False

            def login(self, username, password):
                if transport.fail_login:
                    raise imaplib.IMAP4.error("Authentication failed")
                transport.logins.append(("imap", username, password))
                return "OK", []

            def select(self, mailbox, *, readonly):
                assert mailbox == "INBOX" and readonly is True
                transport.reads.append(mailbox)
                return "OK", [str(len(transport.inbox)).encode()]

            def response(self, name):
                assert name in {"UIDVALIDITY", "UIDNEXT"}
                return name, [str(1 if name == "UIDVALIDITY" else len(transport.inbox) + 1).encode()]

            def uid(self, operation, *arguments):
                if operation == "fetch":
                    assert arguments[1] == "(BODY.PEEK[])"
                    return "OK", [(b"message", transport.inbox[int(arguments[0]) - 1].as_bytes())]
                assert operation == "search"
                criteria = [str(value) for value in arguments[1:]]
                matched = []
                for uid, message in enumerate(transport.inbox, 1):
                    for direction in ("SINCE", "BEFORE"):
                        if direction in criteria:
                            date = datetime.strptime(criteria[criteria.index(direction) + 1], "%d-%b-%Y").date()
                            message_date = message["Date"].datetime.date()
                            if message_date < date if direction == "SINCE" else message_date >= date:
                                break
                    else:
                        direction = None
                    if direction is not None:
                        continue
                    if "UID" in criteria:
                        first = int(criteria[criteria.index("UID") + 1].split(":")[0])
                        if uid < first:
                            continue
                    if "FROM" in criteria and criteria[criteria.index("FROM") + 1].strip('"') not in str(message["From"]):
                        continue
                    if "HEADER" in criteria:
                        checks = [(criteria[index + 1], criteria[index + 2].strip('"'))
                                  for index, value in enumerate(criteria) if value == "HEADER"]
                        matches = [value in str(message.get(name, "")) for name, value in checks]
                        if not (any(matches) if "OR" in criteria else all(matches)):
                            continue
                    matched.append(str(uid))
                return "OK", [" ".join(matched).encode()]

        def http_sync(client, request):
            return transport.http(request)

        async def http_async(client, request):
            return transport.http(request)

        def no_socket(connection, address):
            pytest.fail("An un-faked network transport attempted a connection.")

        monkeypatch.setattr("smtplib.SMTP", SMTP)
        monkeypatch.setattr("smtplib.SMTP_SSL", SMTP)
        monkeypatch.setattr("imaplib.IMAP4_SSL", IMAP)
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", http_sync)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", http_async)
        monkeypatch.setattr(socket.socket, "connect", no_socket)


class OwnerMail:
    def __init__(self, client, transport, venture, audience):
        self.client, self.transport, self.venture, self.audience = client, transport, venture, audience
        self.path = f"/ventures/{venture['id']}"
        self.journey = "/api/v1" + self.path + "/gtm/journey"
        self.jobs = []

    def request(self, method, path, payload=None, *, statuses=(200, 201, 202)):
        response = self.client.request(method, path, json=payload)
        if self.transport.password in response.text:
            pytest.fail("Your HTTP response exposes your saved mailbox password.")
        assert response.status_code in statuses, (response.status_code, response.text)
        return response.json()

    def settings(self, **changes):
        return self.request("PUT", "/local/settings/smtp", {
            "host": "smtp.sender.example", "port": 587, "user": "owner@sender.example",
            "password": self.transport.password, "sender_name": "Local Owner",
            "postal_address": "24 Example Road, Test City, NY 10001",
            "imap_host": "imap.sender.example", "imap_port": 993,
            "booking_link": "https://calendar.example/owner", **changes})

    def switch(self, name, on):
        return self.request("PUT", f"/local/capabilities/{name}", {"on": on})

    def chat(self, text):
        response = self.request("POST", self.path + "/chat", {"message": text})
        self.drain()
        return response

    def action(self, action, payload):
        response = self.request("POST", self.path + "/chat/confirm", {
            "action": action, "payload": payload, "idempotency_key": secrets.token_hex(16)})
        self.drain()
        return response

    def view(self):
        return self.request("GET", self.journey)

    def import_contact(self, index=1):
        address = f"person{index}@{self.audience['domain']}"
        path = self.path + "/gtm/contacts/import"
        preview = self.request("POST", path + "/preview", {
            "format": "csv", "text": f"name,email,country\n{self.audience['name']},{address},US\n"})
        assert preview["valid_rows"] == 1
        committed = self.request("POST", path, {"preview_id": preview["preview_id"],
            "lawful_basis_attested": True, "lawful_basis": "consent"})
        return committed["contact_ids"][0], address

    def draft(self, contact_id):
        response = self.action("outreach.draft", {"run": True, "review_only": True,
            "contact_id": contact_id, "request": "Write a short invitation to discuss the problem. Do not send it."})
        drafts = [draft for draft in self.view()["drafts"] if draft.get("contact_id") == contact_id]
        assert drafts, ("Your requested email must be saved as a usable draft.", response)
        self.chat("Show drafts")
        return next(draft for draft in self.view()["drafts"] if draft["doc_id"] == drafts[-1]["doc_id"]), response

    def send(self, draft):
        return self.chat(f"Send it to {draft['to']} now.")

    def drain(self):
        from app.core.jobs import lease
        from app.state import db
        from app.worker.main import _run_one

        for iteration in range(40):
            with db()() as session:
                jobs = lease(session, "local-mail-flow", kinds=["task.run", "gtm.outreach.retry",
                    "gtm.gmail.read", "gtm.journey.maintain"], n=1)
                session.commit()
            if not jobs:
                return
            asyncio.run(_run_one(db(), jobs[0], {(jobs[0].id, jobs[0].attempts)}))
            self.jobs.append(jobs[0].kind)
        pytest.fail("Your local mail worker did not finish its queued work.")

    def tick(self):
        from app.worker.main import _run_scheduler

        _run_scheduler()
        self.drain()

    def advance(self, monkeypatch, days):
        from core import clock

        moment = clock.utcnow() + timedelta(days=days)
        monkeypatch.setattr(clock, "_clock", lambda: moment)
        self.tick()

    def allow_followups(self, *, cap=15):
        self.switch("send_email", True)
        self.switch("background_work", True)
        self.request("PATCH", self.journey + "/settings", {"allow_sending": True,
            "auto_approve_first_emails": False, "followup_days": [3, 7]})
        self.request("PUT", self.path + "/autopilot", {"allow_send": True, "send_max_per_day": cap})

    def recorded_send(self, contact_id):
        return self.request("POST", self.journey + f"/prospects/{contact_id}/sent", {})


@pytest.fixture
def owner_mail(local_owner_app, monkeypatch, audience, request):
    """Seed the mail schema separately so the startup regression cannot mask mail failures."""
    from app import state
    from app.community.schema import ensure_community_schema
    from app.coding_agents_consent import ensure_schema as ensure_consent_schema
    from app.db.models import Base
    from app.db.session import _register_sqlite_pragmas, make_session_factory
    from app.gtm.supymem.schema import ensure_supymem_schema
    from app.inbox.store import ensure_schema as ensure_inbox_schema
    from app.marketing.media.schema import ensure_marketing_media_schema
    from app.worker.handlers import install_default_handlers

    engine = create_engine(state.resolve_database_url(), connect_args={"check_same_thread": False})
    _register_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)
    for ensure_schema in (ensure_community_schema, ensure_consent_schema, ensure_supymem_schema,
                          ensure_inbox_schema, ensure_marketing_media_schema):
        ensure_schema(engine)
    state.install_append_only_triggers(engine)
    state.set_factory(make_session_factory(engine))
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "IMAP_HOST", "IMAP_PORT",
                 "SUPYMEM_SMTP_HOST", "SUPYMEM_SMTP_PORT", "SUPYMEM_SMTP_USERNAME", "SUPYMEM_SMTP_PASSWORD",
                 "SUPYMEM_IMAP_HOST", "SUPYMEM_IMAP_PORT", "SENDER_NAME", "SENDER_POSTAL_ADDRESS",
                 "SUPYMEM_PUBLIC_URL", "OUTREACH_UNSUB_SECRET"):
        monkeypatch.delenv(name, raising=False)
    transport = MailTransport()
    transport.install(monkeypatch)
    install_default_handlers()
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        created = client.post("/ventures", json={"raw_idea": audience["idea"], "start": False})
        assert created.status_code == 201, created.text
        venture = created.json()
        client.headers["X-Workspace-Id"] = venture["workspace_id"]
        owner = OwnerMail(client, transport, venture, audience)
        owner.request("POST", "/local/providers", {"provider": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1", "models": {"chat": "offline-fixture"}})
        missing_password = getattr(request.node, "callspec", None) and request.node.callspec.params.get("field_name") == "password"
        owner.settings(**({"password": None} if missing_password else {}))
        answers = {"making": audience["idea"], "audience": audience["audience"],
                   "problem": audience["problem"], "progress": "I have a prototype and no users or revenue yet."}
        for step in range(8):
            context = owner.request("GET", owner.path + "/context")
            if not context.get("question"):
                break
            field_name = context["question"]["field"]
            assert field_name in answers, context
            owner.request("POST", owner.path + "/context/answer", {
                "revision": context["revision"], "field": field_name, "value": answers[field_name]})
        yield owner
        assert not any(host == "supymem.com" or host.endswith(".supymem.com") for host in transport.hosts)
    engine.dispose()


def assert_requested_work(reply):
    assert reply.get("kind") != "confirmation", "Your requested work must not need a confirmation card."
    assert not any(isinstance(action, dict) and action.get("confirmation") for action in reply.get("actions", []))
    assert not re.search(r"\b\d+(?:\.\d+)?\s+credits?\b", reply.get("body_md", ""), re.I)


def test_local_file_database_starts_for_your_mail(local_owner_app, audience, monkeypatch):
    """You can open your local app on its file-backed SQLite database before setting up mail."""
    MailTransport().install(monkeypatch)
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        response = client.post("/ventures", json={"raw_idea": audience["idea"], "start": False})
        assert response.status_code == 201


def test_draft_is_usable_without_sending_permission(owner_mail):
    """You can import a consenting contact and draft while sending is switched off."""
    owner_mail.switch("send_email", False)
    contact_id, address = owner_mail.import_contact()
    draft, reply = owner_mail.draft(contact_id)
    assert draft["to"] == address and draft["subject"] and draft["body"]
    assert_requested_work(reply)
    owner_mail.tick()
    assert owner_mail.transport.sent == []


def test_requested_saved_smtp_send(owner_mail):
    """You switch on sending and ask once; your saved SMTP sends exactly one compliant email."""
    assert owner_mail.switch("send_email", True)["state"] == "ready"
    contact_id, address = owner_mail.import_contact()
    draft, reply = owner_mail.draft(contact_id)
    sent_reply = owner_mail.send(draft)
    assert_requested_work(sent_reply)
    assert len(owner_mail.transport.sent) == 1, sent_reply.get("body_md")
    message = owner_mail.transport.sent[0]
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert str(message["To"]) == address
    assert "Local Owner" in str(message["From"])
    assert "24 Example Road" in body
    assert 'just reply "no thanks"' in body
    assert "I have your address from my own contacts." in body
    assert "supymem.com" not in message.as_string()
    if ("smtp", "owner@sender.example", owner_mail.transport.password) not in owner_mail.transport.logins:
        pytest.fail("Your SMTP connection did not use your saved mailbox password.")
    assert "smtp" in owner_mail.transport.tls
    owner_mail.send(draft)
    assert len(owner_mail.transport.sent) == 1


@pytest.mark.parametrize("capability_name,prompt", [
    ("send_email", "Send it now."), ("read_replies", "Any replies?"),
])
def test_capability_off_names_the_screen_and_switch(owner_mail, capability_name, prompt):
    """You get the exact switch and screen to use, and disabled mail makes no transport call."""
    contact_id, address = owner_mail.import_contact()
    owner_mail.draft(contact_id)
    capability = owner_mail.switch(capability_name, False)
    reply = owner_mail.chat(prompt)
    assert capability["state"] == "off"
    assert capability["next_step"] in reply["body_md"], reply["body_md"]
    assert "Settings > Capabilities" in reply["body_md"]
    assert_requested_work(reply)
    assert owner_mail.transport.sent == [] and owner_mail.transport.reads == []


@pytest.mark.parametrize("capability_name,field_name,label,prompt", [
    ("send_email", "host", "Outgoing mail host", "Send it now."),
    ("send_email", "user", "Mailbox address", "Send it now."),
    ("send_email", "password", "Mail app password", "Send it now."),
    ("send_email", "sender_name", "Sender name", "Send it now."),
    ("send_email", "postal_address", "Postal address", "Send it now."),
    ("read_replies", "imap_host", "Incoming mail host", "Any replies?"),
    ("read_replies", "user", "Mailbox address", "Any replies?"),
    ("read_replies", "password", "Mail app password", "Any replies?"),
])
def test_missing_saved_setting_names_exact_field_and_screen(owner_mail, capability_name, field_name, label, prompt):
    """You can fix every missing mailbox requirement in Keys and connections without an admin or env flag."""
    contact_id, address = owner_mail.import_contact()
    owner_mail.draft(contact_id)
    owner_mail.settings(**{field_name: None if field_name == "password" else ""})
    capability = owner_mail.switch(capability_name, True)
    reply = owner_mail.chat(prompt)
    assert capability["state"] == "needs_setup" and label in capability["missing"]
    assert capability["next_step"] in reply["body_md"], reply["body_md"]
    assert "Settings > Keys and connections > Your mail account" in reply["body_md"]
    assert_requested_work(reply)
    assert owner_mail.transport.sent == [] and owner_mail.transport.reads == []


def test_keys_and_drafts_do_not_authorize_background_sending(owner_mail, monkeypatch):
    """Saving your keys, enabling mail and drafting do not request a send or start a campaign."""
    owner_mail.switch("send_email", True)
    contact_id, address = owner_mail.import_contact()
    owner_mail.draft(contact_id)
    owner_mail.advance(monkeypatch, 7)
    assert owner_mail.transport.sent == []
    assert not owner_mail.jobs


def test_saved_settings_and_mail_views_never_return_or_store_plaintext_password(owner_mail, tmp_path):
    """Your saved app password stays encrypted and never appears in settings, chat or mail views."""
    for path in ("/local/settings/smtp", "/local/capabilities",
                 owner_mail.journey, owner_mail.path + "/chat", owner_mail.path + "/autopilot"):
        owner_mail.request("GET", path)
    for path in tmp_path.rglob("*"):
        if path.is_file() and owner_mail.transport.password.encode() in path.read_bytes():
            pytest.fail("Your mailbox password was stored in plaintext.")


def test_opt_out_recording_suppresses_drafts_and_sends(owner_mail):
    """You log a recipient's opt-out once; the contact stays suppressed for drafts and sending."""
    owner_mail.switch("send_email", True)
    contact_id, address = owner_mail.import_contact()
    draft, reply = owner_mail.draft(contact_id)
    owner_mail.request("POST", owner_mail.journey + f"/prospects/{contact_id}/reply", {
        "text": "Please remove me and do not email me again.", "outcome": "opted_out"})
    owner_mail.send(draft)
    owner_mail.tick()
    from app.db.models import GtmContact, GtmSuppression
    from app.state import db

    with db()() as session:
        contact = session.get(GtmContact, contact_id)
        assert contact.do_not_contact and contact.unsubscribed_at
        assert session.scalar(select(GtmSuppression.id).where(GtmSuppression.email == address))
    assert owner_mail.transport.sent == []


def test_booked_call_requires_a_recorded_send(owner_mail):
    """You cannot mark an unsent invitation as booked; the response tells you to record your send first."""
    contact_id, address = owner_mail.import_contact()
    response = owner_mail.request("POST", owner_mail.journey + f"/prospects/{contact_id}/call-booked", {},
                                  statuses=(400, 409, 422))
    text = json.dumps(response)
    assert "Find customers" in text and "sent" in text.lower(), text
    assert owner_mail.transport.sent == []


def test_recorded_outreach_can_be_marked_booked_without_sending(owner_mail):
    """You record an invitation you sent yourself, then mark its booked call without sending another email."""
    contact_id, address = owner_mail.import_contact()
    owner_mail.recorded_send(contact_id)
    owner_mail.request("POST", owner_mail.journey + f"/prospects/{contact_id}/call-booked", {})
    owner_mail.request("POST", owner_mail.journey + f"/prospects/{contact_id}/call-booked", {})
    records = [record for record in owner_mail.view()["manual_outreach"] if record["id"] == contact_id]
    assert len(records) == 1 and records[0]["sent_at"] and records[0]["booked_at"]
    assert owner_mail.transport.sent == []


@pytest.mark.parametrize("reply_text,outcome", [
    ("Yes, I am interested. Let's talk about a call.", "interested"),
    ("No thanks. Please remove me and do not email me again.", "opted_out"),
])
def test_imap_replies_and_opt_outs_reach_your_saved_contact(owner_mail, reply_text, outcome):
    """You ask for replies; IMAP reads your inbox read-only, attributes the reply and honors an opt-out."""
    owner_mail.switch("send_email", True)
    owner_mail.switch("read_replies", True)
    contact_id, address = owner_mail.import_contact()
    draft, reply = owner_mail.draft(contact_id)
    sent_reply = owner_mail.send(draft)
    assert len(owner_mail.transport.sent) == 1, sent_reply.get("body_md")
    root = str(owner_mail.transport.sent[0]["Message-ID"])
    owner_mail.transport.reply(address, reply_text, root)
    read_reply = owner_mail.chat("Any replies?")
    assert_requested_work(read_reply)
    replies = [reply for reply in owner_mail.view()["results"]["replies"] if reply["contact_id"] == contact_id]
    assert len(replies) == 1 and reply_text in replies[0]["body"]
    assert "imap" in owner_mail.transport.tls and owner_mail.transport.reads
    if ("imap", "owner@sender.example", owner_mail.transport.password) not in owner_mail.transport.logins:
        pytest.fail("Your IMAP connection did not use your saved mailbox password.")
    owner_mail.chat("Any replies?")
    assert len([reply for reply in owner_mail.view()["results"]["replies"] if reply["contact_id"] == contact_id]) == 1
    if outcome == "opted_out":
        from app.db.models import GtmContact, GtmSuppression
        from app.state import db

        with db()() as session:
            assert session.get(GtmContact, contact_id).do_not_contact
            assert session.scalar(select(GtmSuppression.id).where(GtmSuppression.email == address))
        owner_mail.send(draft)
    assert len(owner_mail.transport.sent) == 1


@pytest.mark.parametrize("stop", ["off", "background_off",
    "opted_out", "booked", "none"])
def test_local_worker_sends_only_requested_due_followups(owner_mail, monkeypatch, stop):
    """You opt into follow-ups; the real worker respects due dates, your off switch, opt-outs and booked calls."""
    owner_mail.allow_followups()
    contact_id, address = owner_mail.import_contact()
    draft, reply = owner_mail.draft(contact_id)
    sent_reply = owner_mail.send(draft)
    assert len(owner_mail.transport.sent) == 1, sent_reply.get("body_md")
    owner_mail.advance(monkeypatch, 1)
    assert len(owner_mail.transport.sent) == 1
    if stop == "off":
        owner_mail.switch("send_email", False)
    elif stop == "background_off":
        owner_mail.switch("background_work", False)
    elif stop == "opted_out":
        owner_mail.switch("read_replies", True)
        owner_mail.transport.reply(address, "No thanks. Please stop emailing me.", str(owner_mail.transport.sent[0]["Message-ID"]))
        owner_mail.chat("Any replies?")
    elif stop == "booked":
        owner_mail.request("POST", owner_mail.journey + f"/prospects/{contact_id}/call-booked", {})
    owner_mail.advance(monkeypatch, 3)
    expected = 2 if stop == "none" else 1
    assert len(owner_mail.transport.sent) == expected
    assert "gtm.journey.maintain" in owner_mail.jobs
    owner_mail.tick()
    assert len(owner_mail.transport.sent) == expected
    if stop == "none":
        assert str(owner_mail.transport.sent[-1]["In-Reply-To"]) == str(owner_mail.transport.sent[0]["Message-ID"])
        assert 'just reply "no thanks"' in owner_mail.transport.sent[-1].get_content()


def test_direct_followup_drafting_respects_your_background_switch(owner_mail, monkeypatch):
    from app.autopilot import outreach
    from app.autopilot.authority import founder_scope

    owner_mail.allow_followups()
    contact_id, address = owner_mail.import_contact()
    draft, reply = owner_mail.draft(contact_id)
    owner_mail.send(draft)
    assert len(owner_mail.transport.sent) == 1
    owner_mail.switch("background_work", False)
    owner_mail.advance(monkeypatch, 3)
    with founder_scope(owner_mail.venture["id"], autonomous=True):
        saved = outreach.drafts(owner_mail.venture["id"])
        assert outreach.schedule_followups(owner_mail.venture["id"]) == 0
        assert outreach.drafts(owner_mail.venture["id"]) == saved
    assert len(owner_mail.transport.sent) == 1


def test_owner_daily_send_cap_is_enforced(owner_mail):
    """You set a one-email daily cap; a second requested email remains saved and gives a next step."""
    owner_mail.allow_followups(cap=1)
    first_id, first_address = owner_mail.import_contact(1)
    first, reply = owner_mail.draft(first_id)
    second_id, second_address = owner_mail.import_contact(2)
    second, reply = owner_mail.draft(second_id)
    first_reply = owner_mail.send(first)
    assert len(owner_mail.transport.sent) == 1, first_reply.get("body_md")
    second_reply = owner_mail.send(second)
    assert len(owner_mail.transport.sent) == 1
    assert second_address in {draft["to"] for draft in owner_mail.view()["drafts"] if draft["state"] == "draft"}
    assert re.search(r"tomorrow|daily|limit|cap", second_reply["body_md"], re.I)
    assert re.search(r"tomorrow|Settings|Find customers|ask to send .* after it resets", second_reply["body_md"])
    assert_requested_work(second_reply)


@pytest.mark.parametrize("section", ["smtp", "imap"])
def test_saved_mail_connection_test_uses_tls_without_sending(owner_mail, section):
    """You test each saved connection over TLS using your own password without sending or reading messages."""
    response = owner_mail.request("POST", f"/local/settings/{section}/test")
    assert response["ok"] is True, response
    assert section in owner_mail.transport.tls
    if (section, "owner@sender.example", owner_mail.transport.password) not in owner_mail.transport.logins:
        pytest.fail("Your connection test did not use your saved mailbox password.")
    assert owner_mail.transport.sent == [] and owner_mail.transport.reads == []


@pytest.mark.parametrize("section", ["smtp", "imap"])
def test_mail_authentication_failure_names_settings_and_password(owner_mail, section):
    """A mailbox refusing your login tells you to correct the app password on Keys and connections."""
    owner_mail.transport.fail_login = True
    response = owner_mail.request("POST", f"/local/settings/{section}/test")
    assert response["ok"] is False
    assert "Keys and connections" in response["next_step"] and "Mail app password" in response["next_step"]
    host_field = "Incoming mail host" if section == "imap" else "Outgoing mail host"
    assert host_field in response["next_step"]
    assert owner_mail.transport.sent == [] and owner_mail.transport.reads == []


@pytest.mark.parametrize("removed", ["disclosure", "opt_out"])
def test_editing_your_draft_preserves_required_disclosure_and_opt_out(owner_mail, removed):
    """Your unsafe edit is repaired or refused without losing the required disclosure, opt-out or address."""
    contact_id, address = owner_mail.import_contact()
    draft, reply = owner_mail.draft(contact_id)
    required = "I have your address from my own contacts." if removed == "disclosure" else 'If this is not relevant, just reply "no thanks" and I will not email you again.'
    owner_mail.request("PATCH", owner_mail.journey + f"/drafts/{draft['doc_id']}", {
        "expected_revision": draft["revision"], "decision": "draft", "body": draft["body"].replace(required, "")},
        statuses=(200, 422))
    saved = next(item for item in owner_mail.view()["drafts"] if item["doc_id"] == draft["doc_id"])
    assert required in saved["body"] and "24 Example Road" in saved["body"]
    assert owner_mail.transport.sent == []


def test_mail_api_cannot_accept_guessed_recipient_metadata(owner_mail):
    """You cannot bypass the no-guessed-emails rule by submitting guessed recipient metadata through the API."""
    contact_id, address = owner_mail.import_contact()
    owner_mail.request("PATCH", owner_mail.journey + f"/prospects/{contact_id}", {
        "lawful_basis": "consent", "email_confidence": "guess",
        "email_route": {"route_type": "guess", "email": address}}, statuses=(422,))
    from app.db.models import GtmContact
    from app.state import db

    with db()() as session:
        contact = session.get(GtmContact, contact_id)
        assert contact.email == address and contact.email_confidence != "guess"
        assert not json.loads(contact.enrichment_json or "{}").get("email_route")
    assert owner_mail.transport.sent == []
